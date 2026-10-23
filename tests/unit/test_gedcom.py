"""Offline tests for the GEDCOM adapter.

The fixture is the example universe's GEDCOM rendering and every inline
snippet uses the same universe or bare placeholders (docs/EXAMPLES.md).
"""

import hashlib
import unicodedata
from pathlib import Path

import pytest

from yizkr.claims import (
    ArtifactRef,
    ClaimBatch,
    MonolingualText,
    structure_problems,
    validate_against_manifest,
)
from yizkr.pipeline.gedcom import (
    ADAPTER_IDENTIFIER,
    GEDCOM_LANGUAGES,
    UNDETERMINED,
    WEDDING_TYPES,
    adapt_gedcom,
    format_unsupported_tags,
    parse_gedcom,
    parse_gedcom_date,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "example-family.ged"
# the export's content key, which every claim it gives cites (#102); the
# inline snippets below cite it too, as a stand-in for their own bytes
EXPORT = ArtifactRef.from_path(FIXTURE)


def fixture_result():
    return adapt_gedcom(FIXTURE.read_text(), artifact=EXPORT)


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
        assert [r.xref for r in indi] == [f"@I{n}@" for n in range(1, 18)]
        birth = indi[2].first("BIRT")
        assert birth.text("DATE") == "15 FEB 1862"
        assert birth.text("PLAC") == "Flörburg, Hesse, Germany"

    def test_record_lines_round_trip(self):
        roots = parse_gedcom("0 @I1@ INDI\n1 NAME A /B/\n2 SOUR x\n")
        assert roots[0].lines() == "0 @I1@ INDI\n1 NAME A /B/\n2 SOUR x"

    def test_cont_concatenation(self):
        roots = parse_gedcom(
            "0 @N1@ NOTE first\n1 CONC  part\n1 CONT second line\n"
        )
        assert roots[0].value == "first part\nsecond line"

    def test_bad_line_raises_with_number(self):
        with pytest.raises(ValueError, match="line 2"):
            parse_gedcom("0 HEAD\nnot a gedcom line\n")


class TestDates:
    def test_exact_precisions(self):
        assert parse_gedcom_date("15 FEB 1862")[0] == {
            "type": "time",
            "year": 1862,
            "month": 2,
            "day": 15,
            "precision": "day",
        }
        assert parse_gedcom_date("AUG 1939")[0]["precision"] == "month"
        assert parse_gedcom_date("1919")[0]["precision"] == "year"

    def test_abt_maps_to_circa(self):
        value, quals = parse_gedcom_date("ABT 1833")
        assert value["year"] == 1833 and value["precision"] == "year"
        assert quals[0]["property"] == "sourcing_circumstances"
        assert quals[0]["value"]["value"]["key"] == "circa"

    def test_bef_maps_to_latest_date(self):
        value, quals = parse_gedcom_date("BEF 1 OCT 1939")
        assert value["day"] == 1
        assert quals == [{"property": "latest_date", "value": value}]

    def test_between_maps_to_range(self):
        value, quals = parse_gedcom_date("BET 1938 AND 1939")
        assert value["year"] == 1938
        assert {q["property"] for q in quals} == {
            "earliest_date",
            "latest_date",
        }

    def test_garbage_is_never_guessed(self):
        assert parse_gedcom_date("SOMEDAY MAYBE") == (None, [])
        assert parse_gedcom_date("ABT UNKNOWNVILLE") == (None, [])

    def test_full_month_names(self):
        # hand-maintained trees spell months out; still unambiguous
        assert parse_gedcom_date("16 March 1909")[0] == {
            "type": "time",
            "year": 1909,
            "month": 3,
            "day": 16,
            "precision": "day",
        }
        assert parse_gedcom_date("September 1939")[0]["month"] == 9
        assert parse_gedcom_date("3 Sept 1939")[0]["month"] == 9

    def test_month_first_order_with_named_month(self):
        assert parse_gedcom_date("December 15 1905")[0] == {
            "type": "time",
            "year": 1905,
            "month": 12,
            "day": 15,
            "precision": "day",
        }

    def test_german_month_names(self):
        assert parse_gedcom_date("16 März 1909")[0] == {
            "type": "time",
            "year": 1909,
            "month": 3,
            "day": 16,
            "precision": "day",
        }
        assert parse_gedcom_date("24 Oktober 1916")[0]["month"] == 10

    def test_lowercase_keywords(self):
        value, quals = parse_gedcom_date("abt 1833")
        assert value["year"] == 1833
        assert quals[0]["property"] == "sourcing_circumstances"


def item_of(value) -> str:
    """The local id or vocabulary key an item value names."""
    ref = value.value
    return getattr(ref, "local_id", None) or ref.key


def roles(claim) -> list[str]:
    return [
        q.value.value.key
        for q in claim.qualifiers
        if q.property == "object_has_role"
    ]


class TestAdapter:
    def test_batch_is_manifest_valid_and_deterministic(self):
        first = fixture_result()
        # the batch holds no claim of a retired property, so the manifest
        # check passes as it stands (ADR-0036 retires `spouse`)
        assert validate_against_manifest(first.batch) == []
        assert not [c for c in first.batch.claims if c.property == "spouse"]
        second = fixture_result()
        assert first.batch.claims == second.batch.claims
        assert first.batch.entities == second.batch.entities
        assert (
            first.batch.extraction_run.model_identifier == ADAPTER_IDENTIFIER
        )

    def test_entities_carry_names_and_aliases(self):
        batch = fixture_result().batch
        caecilie = next(e for e in batch.entities if e.id == "i2")
        assert caecilie.label == "Cäcilie Tarle"
        assert caecilie.aliases == ["Cäcilie Löbig", "Cilli"]
        assert caecilie.kind == "human"
        # places become entities too, deduplicated by name
        floerburg = [
            e for e in batch.entities if e.label == "Flörburg, Hesse, Germany"
        ]
        assert len(floerburg) == 1

    def test_uncertainty_flows_into_claims(self):
        batch = fixture_result().batch
        (death,) = claims_for(batch, "i5", "date_of_death")  # AFT 1945
        assert death.qualifiers[0].property == "earliest_date"
        (birth,) = claims_for(batch, "i2", "date_of_birth")  # ABT 1833
        assert birth.qualifiers[0].property == "sourcing_circumstances"
        # the studio portrait is dated only to the 1890s
        (portrait,) = claims_for(batch, "i1", "image")
        assert {q.property for q in portrait.qualifiers} == {
            "point_in_time",
            "earliest_date",
            "latest_date",
        }

    def test_parentage_and_marriage(self):
        batch = fixture_result().batch
        (father,) = claims_for(batch, "i8", "father")
        assert father.value.value.local_id == "i3"
        (mother,) = claims_for(batch, "i8", "mother")
        assert mother.value.value.local_id == "i4"
        # Sebastian and Hildegard's family is a marriage, whose couple
        # take part in it as spouses, started by its civil wedding
        kinds = {e.id: e.kind for e in batch.entities}
        assert kinds["f2"] == "marriage"
        spouses = claims_for(batch, "f2", "participant")
        assert [(item_of(c.value), roles(c)) for c in spouses] == [
            ("i3", ["spouse"]),
            ("i4", ["spouse"]),
        ]
        (started,) = claims_for(batch, "f2", "started_by")
        assert item_of(started.value) == "f2-civil-wedding"
        assert kinds["f2-civil-wedding"] == "civil_wedding"
        (date,) = claims_for(batch, "f2-civil-wedding", "point_in_time")
        assert (date.value.year, date.value.month, date.value.day) == (
            1905,
            12,
            15,
        )
        (place,) = claims_for(batch, "f2-civil-wedding", "location")
        places = {e.id: e.label for e in batch.entities}
        assert places[item_of(place.value)] == "Flörburg, Hesse, Germany"

    def test_divorce_is_an_event_that_ends_the_marriage(self):
        # Leopold (i11) and Agnes (i17): married 8 Feb 1947, divorced
        # 2 Jan 1957; the union is ended by its divorce, which the DIV
        # dates
        batch = fixture_result().batch
        (ended,) = claims_for(batch, "f5", "ended_by")
        assert item_of(ended.value) == "f5-divorce"
        (date,) = claims_for(batch, "f5-divorce", "point_in_time")
        assert (date.value.year, date.value.month, date.value.day) == (
            1957,
            1,
            2,
        )
        assert date.qualifiers == []

    def test_felix_and_idas_family_is_a_partnership(self):
        # the export gives Felix and Ida no MARR (ADR-0036 §4)
        batch = fixture_result().batch
        (union,) = [e for e in batch.entities if e.id == "f3"]
        assert union.kind == "partnership"
        assert union.label == "Partnership of Felix Löbig and Ida Sörne"
        partners = claims_for(batch, "f3", "participant")
        assert [(item_of(c.value), roles(c)) for c in partners] == [
            ("i6", ["partner"]),
            ("i7", ["partner"]),
        ]
        assert claims_for(batch, "f3", "started_by") == []

    def test_no_end_is_inferred_from_a_partners_death(self):
        # Eugen's and Cäcilie's deaths end no union: only the divorce of
        # Leopold and Agnes ends one
        batch = fixture_result().batch
        ended = [c for c in batch.claims if c.property == "ended_by"]
        assert [c.subject.local_id for c in ended] == ["f5"]

    def test_the_fixture_counts_its_unions(self):
        assert fixture_result().outcomes == {
            "NAME bracketed part": 1,
            "union: marriage": 4,
            "union: wedding": 2,
            "union: civil wedding": 2,
            "union: partnership": 1,
            "union: divorce": 1,
        }

    def test_every_claim_carries_verbatim_gedcom_excerpt(self):
        batch = fixture_result().batch
        for claim in batch.claims:
            excerpt = claim.source.transcription_excerpt
            assert claim.source.artifact == EXPORT
            assert excerpt is not None and len(excerpt.text) <= 400
            # monolingual text values forbid newlines
            assert "\n" not in excerpt.text
        (birth,) = claims_for(batch, "i3", "date_of_birth")
        assert "2 DATE 15 FEB 1862" in birth.source.transcription_excerpt.text

    def test_the_adapter_cites_the_key_it_is_given(self):
        """The adapter cites the export by the key its caller hashed from
        the file's bytes, never by one it makes from the text it reads: a
        snippet adapted under the fixture's key cites that key (#102)."""
        snippet = "0 @I1@ INDI\n1 NAME A /B/\n1 BIRT\n2 DATE 1871\n0 TRLR\n"
        batch = adapt_gedcom(snippet, artifact=EXPORT).batch
        assert batch.claims
        assert all(claim.source.artifact == EXPORT for claim in batch.claims)

    def test_unparseable_date_warns_instead_of_guessing(self):
        result = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 BIRT\n2 DATE SOMEDAY MAYBE\n"
            "0 TRLR\n",
            artifact=EXPORT,
        )
        assert any("SOMEDAY MAYBE" in warning for warning in result.warnings)
        assert claims_for(result.batch, "i1", "date_of_birth") == []

    def test_fixture_dates_all_parse(self):
        assert fixture_result().warnings == []

    def test_unsupported_tags_are_counted_not_silent(self):
        result = fixture_result()
        assert result.unsupported_tags.get("EDUC") == 1
        assert "OCCU" not in result.unsupported_tags
        # the envelope (HEAD/TRLR and its GEDC/CHAR/SOUR children) is
        # structure, not dropped genealogy — never reported
        for envelope in ("HEAD", "TRLR", "GEDC", "CHAR", "SOUR"):
            assert envelope not in result.unsupported_tags


class TestVocabulary:
    """Item-10 fields: burial, occupation, cause of death, child count."""

    def test_burial_place_with_point_in_time(self):
        batch = fixture_result().batch
        (burial,) = claims_for(batch, "i1", "burial_place")
        place = next(
            e for e in batch.entities if e.id == burial.value.value.local_id
        )
        assert place.label == "Friedhof Flörburg, Hesse, Germany"
        assert place.kind == "place"
        (qualifier,) = burial.qualifiers
        assert qualifier.property == "point_in_time"
        assert (qualifier.value.year, qualifier.value.day) == (1909, 20)

    def test_undated_burial_has_no_qualifiers(self):
        batch = fixture_result().batch
        (burial,) = claims_for(batch, "i8", "burial_place")
        place = next(
            e for e in batch.entities if e.id == burial.value.value.local_id
        )
        assert place.label == "Stupfield, Victoria, Australia"
        assert not burial.qualifiers

    def test_occupation_becomes_verbatim_item(self):
        batch = fixture_result().batch
        (occupation,) = claims_for(batch, "i1", "occupation")
        entity = next(
            e
            for e in batch.entities
            if e.id == occupation.value.value.local_id
        )
        assert entity.label == "Master hatter"
        assert entity.kind == "occupation_class"

    def test_cause_of_death_stays_verbatim(self):
        batch = fixture_result().batch
        (cause,) = claims_for(batch, "i1", "cause_of_death")
        assert cause.value.value.text == "Dropsy"
        assert "2 CAUS Dropsy" in cause.source.transcription_excerpt.text

    def test_number_of_children_is_a_quantity(self):
        batch = fixture_result().batch
        (count,) = claims_for(batch, "i1", "number_of_children")
        assert count.value.type == "quantity"
        assert count.value.amount == 3

    def test_no_sibling_claims_are_derived(self):
        # the export asserts parentage, never siblinghood (docs/MODELING.md)
        batch = fixture_result().batch
        assert not [c for c in batch.claims if c.property == "sibling"]

    def test_sex_maps_to_sex_or_gender(self):
        # ADR-0014: SEX -> sex_or_gender (male/female vocab), for the
        # kinship calculus. i1 is M, i2 is F in the fixture; i13 is U.
        batch = fixture_result().batch
        (eugen_sex,) = claims_for(batch, "i1", "sex_or_gender")
        assert eugen_sex.value.value.key == "male"
        (caecilie_sex,) = claims_for(batch, "i2", "sex_or_gender")
        assert caecilie_sex.value.value.key == "female"
        assert not claims_for(batch, "i13", "sex_or_gender")

    def test_burial_without_place_warns_and_skips(self):
        result = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 BURI\n2 DATE 1900\n0 TRLR\n",
            artifact=EXPORT,
        )
        assert any(
            "BURI without a PLAC that names a place" in w
            for w in result.warnings
        )
        assert not claims_for(result.batch, "i1", "burial_place")

    def test_dated_and_placed_occupation_carries_qualifiers(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Sebastian /Löbig/\n1 OCCU Hatter\n"
            "2 DATE 1919\n2 PLAC Flörburg\n0 TRLR\n",
            artifact=EXPORT,
        ).batch
        (occupation,) = claims_for(batch, "i1", "occupation")
        quals = {q.property: q for q in occupation.qualifiers}
        assert quals["point_in_time"].value.year == 1919
        place = next(
            e
            for e in batch.entities
            if e.id == quals["location"].value.value.local_id
        )
        assert place.label == "Flörburg"

    def test_unparseable_child_count_warns_not_guesses(self):
        result = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 NCHI several\n0 TRLR\n",
            artifact=EXPORT,
        )
        assert any(
            "NCHI" in w and "not a number" in w for w in result.warnings
        )
        assert not claims_for(result.batch, "i1", "number_of_children")


class TestTypedNames:
    """Typed, referenced name claims alongside the flat label/alias index."""

    def _name(self, batch, subject, prop):
        return [c.value.value.text for c in claims_for(batch, subject, prop)]

    def test_name_type_birth_and_married(self):
        batch = fixture_result().batch
        assert self._name(batch, "i2", "birth_name") == ["Cäcilie Tarle"]
        assert self._name(batch, "i2", "married_name") == ["Cäcilie Löbig"]

    def test_nick_subtag_becomes_nickname(self):
        batch = fixture_result().batch
        assert self._name(batch, "i2", "nickname") == ["Cilli"]

    def test_a_bracketed_part_is_an_alias_and_no_nickname(self):
        # "Ida (Peppi) Sörne": a bracket may hold a nickname, a maiden
        # name or a note, so "Peppi" is an alias and an unsplit form of
        # Ida's record, and no typed-name claim (ADR-0035 §6)
        batch = fixture_result().batch
        assert self._name(batch, "i7", "nickname") == []
        ida = next(e for e in batch.entities if e.id == "i7")
        assert ida.label == "Ida Sörne"
        assert "Peppi" in ida.aliases
        assert ida.structure.unsplit == ["Peppi"]
        assert ida.structure.nicknames == []

    def test_a_quoted_part_stays_a_nickname(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Cäcilie 'Cilli' /Tarle/\n0 TRLR\n",
            artifact=EXPORT,
        ).batch
        assert self._name(batch, "i1", "nickname") == ["Cilli"]
        (caecilie,) = batch.entities
        assert caecilie.label == "Cäcilie 'Cilli' Tarle"
        assert caecilie.structure.nicknames == ["Cilli"]
        assert [g.written for g in caecilie.structure.given] == ["Cäcilie"]

    def test_name_type_immigrant_is_official(self):
        batch = fixture_result().batch
        assert self._name(batch, "i8", "official_name") == ["Gustave Lobig"]

    def test_name_type_official(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Gustav /Löbig/\n"
            "1 NAME Gustave /Lobig/\n2 TYPE official\n0 TRLR\n",
            artifact=EXPORT,
        ).batch
        assert self._name(batch, "i1", "official_name") == ["Gustave Lobig"]

    def test_marnm_tag_becomes_married_name(self):
        batch = fixture_result().batch
        assert "Ida Löbig" in self._name(batch, "i7", "married_name")

    def test_name_claims_carry_a_reference(self):
        batch = fixture_result().batch
        for prop in (
            "birth_name",
            "married_name",
            "nickname",
            "official_name",
        ):
            for claim in [c for c in batch.claims if c.property == prop]:
                assert claim.source.artifact == EXPORT
                assert claim.value.type == "monolingual"

    def test_plain_name_emits_no_name_claim(self):
        # a person with an untyped, unquoted name stays label/alias only
        batch = fixture_result().batch
        for prop in (
            "birth_name",
            "married_name",
            "nickname",
            "official_name",
        ):
            assert not claims_for(batch, "i5", prop)  # Rahel Löbig

    def test_label_and_aliases_unchanged_by_typing(self):
        # the two-layer design: typing adds claims, never disturbs the
        # label/alias match index, which holds every name form, a NICK's
        # included
        batch = fixture_result().batch
        caecilie = next(e for e in batch.entities if e.id == "i2")
        assert caecilie.label == "Cäcilie Tarle"
        assert caecilie.aliases == ["Cäcilie Löbig", "Cilli"]

    def test_explicit_type_gedcom_maiden_and_aka(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Hildegard /Laudler/\n2 TYPE maiden\n"
            "1 NAME Hilde /Löbig/\n2 TYPE aka\n0 TRLR\n",
            artifact=EXPORT,
        ).batch
        assert self._name(batch, "i1", "birth_name") == ["Hildegard Laudler"]
        # an 'aka' name is recorded whole as a nickname
        assert self._name(batch, "i1", "nickname") == ["Hilde Löbig"]


RESI_GEDCOM = """\
0 @I1@ INDI
1 NAME Clara /Lobig/
1 RESI
2 DATE 1938
2 PLAC Victoria, Australia
1 RESI
2 PLAC 31 Juniper Street, Stupfield, Victoria, Australia
1 RESI
2 DATE SOMEDAY MAYBE
2 PLAC Nowhere
1 RESI
2 DATE 1952
0 TRLR
"""


class TestResidence:
    def test_dated_residence_carries_point_in_time(self):
        batch = adapt_gedcom(RESI_GEDCOM, artifact=EXPORT).batch
        dated = [
            c
            for c in claims_for(batch, "i1", "residence")
            if any(q.property == "point_in_time" for q in c.qualifiers)
        ]
        assert len(dated) == 1
        (qualifier,) = dated[0].qualifiers
        assert qualifier.value.year == 1938
        place = next(
            e for e in batch.entities if e.id == dated[0].value.value.local_id
        )
        assert place.label == "Victoria, Australia"
        assert place.kind == "place"

    def test_undated_residence_has_no_qualifiers(self):
        batch = adapt_gedcom(RESI_GEDCOM, artifact=EXPORT).batch
        undated = [
            c for c in claims_for(batch, "i1", "residence") if not c.qualifiers
        ]
        # the unparsed-date residence also ends up bare (date omitted)
        assert len(undated) == 2

    def test_unparsed_residence_date_warns_and_omits(self):
        result = adapt_gedcom(RESI_GEDCOM, artifact=EXPORT)
        assert any(
            "SOMEDAY MAYBE" in w and "point_in_time omitted" in w
            for w in result.warnings
        )

    def test_residence_without_place_warns_and_skips(self):
        result = adapt_gedcom(RESI_GEDCOM, artifact=EXPORT)
        assert any(
            "RESI without a PLAC that names a place" in w
            for w in result.warnings
        )
        assert len(claims_for(result.batch, "i1", "residence")) == 3


# A level-0 SOUR (cited upstream source) + REPO sidecar, an OBJE media
# object, and a person whose NAME and BIRT both carry a SOUR citation with
# a PAGE.
CENSUS_GEDCOM = """\
0 HEAD
1 SOUR SomeSoftware
1 CHAR UTF-8
0 @S1@ SOUR
1 TITL Birth register, Standesamt Flörburg
1 REPO @R1@
0 @R1@ REPO
1 NAME Standesamt Flörburg
0 @O1@ OBJE
1 FILE https://example.test/school-report-1930.jpg
2 FORM jpg
2 TITL School report for Rudolf Löbig
1 DATE 1930
0 @I1@ INDI
1 NAME Rudolf /Löbig/
2 GIVN Rudolf
2 SURN Löbig
2 SOUR @S1@
3 PAGE Vol 3 p 1
3 _APID 1,2::3
1 OBJE @O1@
1 BIRT
2 DATE 24 OCT 1916
2 SOUR @S1@
3 PAGE Vol 3 p 1
1 EDUC Städtische Realschule Flörburg
0 TRLR
"""


class TestUnsupportedCensus:
    """The census reports the genuine unmapped tail, at every depth."""

    def test_census_reports_the_unmapped_tail_only(self):
        tags = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).unsupported_tags
        # REPO and EDUC are genuinely unmapped, at level 0 and nested — both
        # reported (the old direct-children scan would have missed REPO).
        # The birth's citation is folded into its claim, and the school
        # report is mapped (ADR-0021), so neither the source's record nor
        # the object's is counted; the NAME's citation is, as the untyped
        # NAME gives no claim to fold it into (#324)
        assert tags == {"REPO": 1, "EDUC": 1, "NAME.SOUR": 1}

    def test_handled_and_dropped_subtrees_are_not_itemised(self):
        # a handled/dropped tag stands for its whole branch: the citation's
        # PAGE/_APID and the media object's FILE/TITL are never itemised
        tags = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).unsupported_tags
        assert "PAGE" not in tags and "_APID" not in tags
        assert "TITL" not in tags and "FILE" not in tags

    def test_name_parts_and_envelope_are_not_dropped(self):
        tags = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).unsupported_tags
        # GIVN/SURN restate the NAME value the adapter reads — not dropped
        assert "GIVN" not in tags and "SURN" not in tags
        # HEAD/TRLR envelope (and the header's own CHAR) never counted
        assert "HEAD" not in tags and "TRLR" not in tags
        assert "CHAR" not in tags

    def test_the_fixture_counts_only_its_unmapped_tail(self):
        # its typed NAMEs, its MARR places and its dated DIV are all read:
        # only the EDUC and the NOTE go unmapped
        assert fixture_result().unsupported_tags == {"EDUC": 1, "NOTE": 1}

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            # a NICK is read beneath a NAME, not beside it
            (["1 NICK Muckel"], {"INDI.NICK": 1}),
            # a DATE is read beneath a BIRT or a DEAT, not beneath a NAME
            (["2 DATE 1836"], {"NAME.DATE": 1}),
            # a CAUS is read beneath a DEAT, not beneath a BURI
            (
                [
                    "1 BURI",
                    "2 PLAC Friedhof Flörburg, Hesse, Germany",
                    "2 CAUS Dropsy",
                ],
                {"BURI.CAUS": 1},
            ),
            # an AGE is read nowhere, so it is named by its tag alone
            (["1 DEAT", "2 DATE 16 MAR 1909", "2 AGE 73"], {"AGE": 1}),
            # a NAME's parts are restated by its value, so they are read
            (["2 NPFX x", "2 SPFX y", "2 NSFX z"], {}),
        ],
    )
    def test_a_tag_is_counted_only_where_it_goes_unread(self, lines, expected):
        # Eugen's record, with (here) lines beneath it or its NAME; a tag
        # the adapter reads beneath other tags is named with its parent's
        # tag where it goes unread (#308)
        text = "\n".join(
            ["0 @I1@ INDI", "1 NAME Eugen /Löbig/", *lines, "0 TRLR", ""]
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == expected

    def test_a_familys_child_count_is_counted(self):
        # (here) Eugen and Cäcilie's family record counts their three
        # children: an NCHI is read beneath an INDI, not beneath a FAM
        text = "\n".join(
            [
                "0 @I1@ INDI",
                "1 NAME Eugen /Löbig/",
                "0 @I2@ INDI",
                "1 NAME Cäcilie /Tarle/",
                "0 @F1@ FAM",
                "1 HUSB @I1@",
                "1 WIFE @I2@",
                "1 NCHI 3",
                "0 TRLR",
                "",
            ]
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {
            "FAM.NCHI": 1
        }


class TestSourceCitations:
    """SOUR citations fold into the claim's reference (ADR-0021 step A)."""

    def test_page_becomes_the_reference_page_number(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).batch
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        assert birth.source.page_number == "Vol 3 p 1"

    def test_cited_source_title_surfaces_in_excerpt(self):
        # the verbatim lines carry only "@S1@"; the resolved upstream source
        # name is appended so the reviewer can read the citation
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).batch
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        excerpt = birth.source.transcription_excerpt.text
        assert "cited: Birth register, Standesamt Flörburg" in excerpt

    def test_claim_without_a_citation_has_no_page_number(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 BIRT\n2 DATE 1900\n0 TRLR\n",
            artifact=EXPORT,
        ).batch
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        assert birth.source.page_number is None


class TestMediaObjects:
    """OBJE becomes a lightweight un-held media item (ADR-0021)."""

    def test_person_images_an_unheld_media_item(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).batch
        (image,) = claims_for(batch, "i1", "image")
        media = next(
            e for e in batch.entities if e.id == image.value.value.local_id
        )
        assert media.kind == "artifact"  # not photograph: bytes not held
        assert media.label == "School report for Rudolf Löbig"
        assert "never fetched" in media.summary

    def test_media_association_carries_the_objects_date(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).batch
        (image,) = claims_for(batch, "i1", "image")
        (qualifier,) = image.qualifiers
        assert qualifier.property == "point_in_time"
        assert qualifier.value.year == 1930

    def test_batch_stays_manifest_valid(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact=EXPORT).batch
        assert validate_against_manifest(batch) == []

    def test_shared_object_is_one_media_item(self):
        gedcom = (
            "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
            "2 TITL Studio portrait of the Löbig family\n"
            "0 @I1@ INDI\n1 NAME Eugen /Löbig/\n1 OBJE @O1@\n"
            "0 @I2@ INDI\n1 NAME Cäcilie /Tarle/\n1 OBJE @O1@\n"
            "0 TRLR\n"
        )
        batch = adapt_gedcom(gedcom, artifact=EXPORT).batch
        media = [e for e in batch.entities if e.kind == "artifact"]
        assert len(media) == 1  # deduped by xref across both people
        assert len(claims_for(batch, "i1", "image")) == 1
        assert len(claims_for(batch, "i2", "image")) == 1

    def test_dangling_pointer_warns_and_skips(self):
        result = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 OBJE @O9@\n0 TRLR\n",
            artifact=EXPORT,
        )
        assert any(
            "OBJE points to unknown object" in w for w in result.warnings
        )
        assert not claims_for(result.batch, "i1", "image")
        # a link that no image claim maps is counted too (#324)
        assert result.unsupported_tags == {"OBJE": 1}


class TestUnsupportedFormat:
    def test_count_is_kept_apart_from_tag(self):
        # "OBJEx5" would read as a corrupt tag; the count must be legible
        assert (
            format_unsupported_tags({"OBJE": 5, "SOUR": 12})
            == "OBJE ×5, SOUR ×12"
        )

    def test_empty_census_is_empty_string(self):
        assert format_unsupported_tags({}) == ""


def family(*lines: str) -> str:
    """Leopold and Agnes's family record, ending (here) with `lines`."""
    return "\n".join(
        [
            "0 @I11@ INDI",
            "1 NAME Leopold /Löbig/",
            "0 @I17@ INDI",
            "1 NAME Agnes /Löbig/",
            "0 @F5@ FAM",
            "1 HUSB @I11@",
            "1 WIFE @I17@",
            *lines,
            "0 TRLR",
            "",
        ]
    )


def eugen(*lines: str) -> str:
    """Eugen's record, ending (here) with `lines`."""
    return "\n".join(
        ["0 @I1@ INDI", "1 NAME Eugen /Löbig/", *lines, "0 TRLR", ""]
    )


def person(xref: str, name: str, *lines: str) -> str:
    """A person's record, with its NAME and then (here) `lines`."""
    return "\n".join(
        [f"0 @{xref}@ INDI", f"1 NAME {name}", *lines, "0 TRLR", ""]
    )


def couple(marriage: str | None = None, divorce: str | None = None) -> str:
    """Leopold and Agnes's family record, its marriage and divorce dated
    (here) as given."""
    lines = []
    if marriage is not None:
        lines += ["1 MARR", f"2 DATE {marriage}"]
    if divorce is not None:
        lines += ["1 DIV", f"2 DATE {divorce}"]
    return family(*lines)


def shown(value) -> tuple | str:
    """A qualifier's value: a time as (year, month, day, precision), a
    vocabulary item by its key, a local entity by its id."""
    if value.type == "time":
        return value.year, value.month, value.day, value.precision
    return item_of(value)


def year(number: int) -> tuple:
    return number, None, None, "year"


# the kinds of a union and its events (ADR-0036)
UNION_KINDS = {
    "partnership", "marriage", "wedding", "civil_wedding",
    "religious_wedding", "divorce", "annulment", "event",
}  # fmt: skip


def unions(batch) -> dict[str, str]:
    """Each union entity and union event of the batch: id -> kind."""
    return {e.id: e.kind for e in batch.entities if e.kind in UNION_KINDS}


def on(batch, subject: str, prop: str | None = None) -> list[tuple]:
    """The claims on `subject` (of `prop`), each as (property, value,
    qualifiers), the qualifiers as sorted (property, value) pairs."""
    return [
        (
            claim.property,
            shown(claim.value),
            sorted((q.property, shown(q.value)) for q in claim.qualifiers),
        )
        for claim in claims_for(batch, subject, prop)
    ]


def dates(text: str, event: str) -> list[tuple]:
    """The date claims of one event of Leopold and Agnes's union, as
    `stated` gives them."""
    batch = adapt_gedcom(text, artifact=EXPORT).batch
    return [
        entry
        for entry in on(batch, event)
        if entry[0] in ("point_in_time", "start_time", "end_time")
    ]


class TestUnions:
    """A family with two partners is a union, bounded by the events its
    MARR, DIV and ANUL give (ADR-0036 §5)."""

    def test_a_family_with_a_marriage_is_a_marriage_of_spouses(self):
        batch = adapt_gedcom(couple("8 FEB 1947"), artifact=EXPORT).batch
        assert unions(batch) == {"f5": "marriage", "f5-wedding": "wedding"}
        (union,) = [e for e in batch.entities if e.id == "f5"]
        assert union.label == "Marriage of Leopold Löbig and Agnes Löbig"
        assert on(batch, "f5", "participant") == [
            ("participant", "i11", [("object_has_role", "spouse")]),
            ("participant", "i17", [("object_has_role", "spouse")]),
        ]
        assert on(batch, "f5", "started_by") == [
            ("started_by", "f5-wedding", [])
        ]
        # the couple take part in the union alone
        assert on(batch, "f5-wedding", "participant") == []

    def test_a_family_without_a_marriage_is_a_partnership(self):
        # the export's Felix and Ida have no MARR (ADR-0036 §4), nor here
        # Leopold and Agnes
        result = adapt_gedcom(family(), artifact=EXPORT)
        assert unions(result.batch) == {"f5": "partnership"}
        assert on(result.batch, "f5", "participant") == [
            ("participant", "i11", [("object_has_role", "partner")]),
            ("participant", "i17", [("object_has_role", "partner")]),
        ]
        assert result.outcomes == {
            "HEAD.LANG absent: und": 1,
            "union: partnership": 1,
        }

    @pytest.mark.parametrize(
        ("kind", "wedding"),
        [
            ("civil", "civil_wedding"),
            ("Civil", "civil_wedding"),
            ("Standesamt", "civil_wedding"),
            ("religious", "religious_wedding"),
            ("Church", "religious_wedding"),
            ("kirchlich", "religious_wedding"),
        ],
    )
    def test_a_type_that_names_the_ceremony_gives_its_kind(
        self, kind, wedding
    ):
        # the judgment has the marriage concluded before the registrar in
        # Flörburg; (here) the family record names the ceremony's kind
        result = adapt_gedcom(
            family("1 MARR", f"2 TYPE {kind}", "2 DATE 8 FEB 1947"),
            artifact=EXPORT,
        )
        event = f"f5-{wedding.replace('_', '-')}"
        assert unions(result.batch) == {"f5": "marriage", event: wedding}
        assert on(result.batch, "f5", "started_by") == [
            ("started_by", event, [])
        ]
        assert result.unsupported_tags == {}

    def test_any_other_type_stays_in_the_excerpt_and_is_counted(self):
        result = adapt_gedcom(
            family("1 MARR", "2 TYPE Elopement", "2 DATE 8 FEB 1947"),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
        }
        assert result.unsupported_tags == {"MARR.TYPE": 1}
        (date,) = claims_for(result.batch, "f5-wedding", "point_in_time")
        assert "2 TYPE Elopement" in date.source.transcription_excerpt.text

    def test_a_further_type_of_another_kind_is_counted(self):
        # the first TYPE that names a ceremony gives the kind
        result = adapt_gedcom(
            family("1 MARR Y", "2 TYPE civil", "2 TYPE religious"),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-civil-wedding": "civil_wedding",
        }
        assert result.unsupported_tags == {"MARR.TYPE": 1}

    def test_marriages_of_one_kind_are_one_wedding(self):
        # (here) two MARRs date the one wedding two ways, and a third, a
        # religious ceremony, is a wedding of its own
        batch = adapt_gedcom(
            family(
                "1 MARR",
                "2 DATE 8 FEB 1947",
                "1 MARR",
                "2 DATE 1947",
                "2 PLAC Flörburg, Hesse, Germany",
                "1 MARR",
                "2 TYPE religious",
                "2 DATE 9 FEB 1947",
            ),
            artifact=EXPORT,
        ).batch
        assert unions(batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
            "f5-religious-wedding": "religious_wedding",
        }
        assert on(batch, "f5", "started_by") == [
            ("started_by", "f5-wedding", []),
            ("started_by", "f5-wedding", []),
            ("started_by", "f5-religious-wedding", []),
        ]
        assert [entry[:2] for entry in on(batch, "f5-wedding")] == [
            ("point_in_time", (1947, 2, 8, "day")),
            ("point_in_time", year(1947)),
            ("location", "pl1"),
        ]
        assert on(batch, "f5-religious-wedding") == [
            ("point_in_time", (1947, 2, 9, "day"), [])
        ]

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "BET 1946 AND 1947",
                [("earliest_date", year(1946)), ("latest_date", year(1947))],
            ),
            ("BEF 1947", [("latest_date", year(1947))]),
            ("AFT 1946", [("earliest_date", year(1946))]),
            ("ABT 1947", [("sourcing_circumstances", "circa")]),
        ],
    )
    def test_a_weddings_range_and_circa_are_its_own(self, date, expected):
        ((prop, _, quals),) = dates(couple(marriage=date), "f5-wedding")
        assert (prop, quals) == ("point_in_time", expected)

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "BET 1956 AND 1957",
                [("earliest_date", year(1956)), ("latest_date", year(1957))],
            ),
            ("AFT 1956", [("earliest_date", year(1956))]),
            ("BEF 1957", [("latest_date", year(1957))]),
            ("ABT 1957", [("sourcing_circumstances", "circa")]),
        ],
    )
    def test_a_divorces_range_and_circa_are_its_own(self, date, expected):
        ((prop, _, quals),) = dates(couple(divorce=date), "f5-divorce")
        assert (prop, quals) == ("point_in_time", expected)

    def test_each_event_keeps_its_own_circa(self):
        # (here) married about 1947 and divorced in 1957: the circa is the
        # wedding's alone
        text = couple("ABT 1947", "1957")
        assert dates(text, "f5-wedding") == [
            (
                "point_in_time",
                year(1947),
                [("sourcing_circumstances", "circa")],
            )
        ]
        assert dates(text, "f5-divorce") == [("point_in_time", year(1957), [])]

    def test_a_marriage_period_is_a_wedding_and_an_end(self):
        # (here) the MARR reads FROM 1947 TO 1957: the wedding is dated by
        # the FROM and an end of unstated kind by the TO
        result = adapt_gedcom(
            couple(marriage="FROM 1947 TO ABT 1957"), artifact=EXPORT
        )
        batch = result.batch
        assert unions(batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
            "f5-event": "event",
        }
        (end,) = [e for e in batch.entities if e.id == "f5-event"]
        assert end.label == (
            "End of the marriage of Leopold Löbig and Agnes Löbig"
        )
        assert on(batch, "f5", "ended_by") == [("ended_by", "f5-event", [])]
        assert on(batch, "f5-wedding") == [("point_in_time", year(1947), [])]
        assert on(batch, "f5-event") == [
            (
                "point_in_time",
                year(1957),
                [("sourcing_circumstances", "circa")],
            )
        ]
        assert result.outcomes["union: event"] == 1

    @pytest.mark.parametrize(
        ("date", "wedding", "end"),
        [
            ("FROM 1947", [("point_in_time", year(1947), [])], None),
            ("TO 1957", [], [("point_in_time", year(1957), [])]),
        ],
    )
    def test_a_marriage_period_of_one_end_dates_that_end(
        self, date, wedding, end
    ):
        batch = adapt_gedcom(couple(marriage=date), artifact=EXPORT).batch
        assert on(batch, "f5", "started_by") == [
            ("started_by", "f5-wedding", [])
        ]
        assert on(batch, "f5-wedding") == wedding
        if end is None:
            assert "f5-event" not in unions(batch)
        else:
            assert on(batch, "f5-event") == end

    def test_a_divorce_period_is_its_own_start_and_end(self):
        # (here) the proceedings ran from 1956 to 2 January 1957, as an
        # imprisonment is dated
        assert dates(
            couple(divorce="FROM BET 1955 AND 1956 TO 2 JAN 1957"),
            "f5-divorce",
        ) == [
            (
                "start_time",
                year(1955),
                [("earliest_date", year(1955)), ("latest_date", year(1956))],
            ),
            ("end_time", (1957, 1, 2, "day"), []),
        ]

    def test_a_divorce_place_is_the_divorces_location(self):
        # (here) the family record places the divorce in Rekau, where the
        # Landgericht Rekau gave its judgment
        result = adapt_gedcom(
            family(
                "1 MARR",
                "2 DATE 8 FEB 1947",
                "2 PLAC Flörburg, Hesse, Germany",
                "1 DIV",
                "2 DATE 2 JAN 1957",
                "2 PLAC Rekau, Hesse, Germany",
            ),
            artifact=EXPORT,
        )
        assert result.unsupported_tags == {}
        assert result.warnings == []
        labels = {e.id: e.label for e in result.batch.entities}
        ((_, place, _),) = on(result.batch, "f5-divorce", "location")
        assert labels[place] == "Rekau, Hesse, Germany"
        ((_, place, _),) = on(result.batch, "f5-wedding", "location")
        assert labels[place] == "Flörburg, Hesse, Germany"

    def test_an_undated_divorce_is_a_divorce(self):
        # (here) the family record says that Leopold and Agnes divorced,
        # but not when: an undated divorce, as an undated death is a death
        # event (ADR-0020 §2)
        result = adapt_gedcom(family("1 DIV Y"), artifact=EXPORT)
        assert result.unsupported_tags == {}
        # a divorce ends a marriage, whose wedding the export does not state
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-divorce": "divorce",
        }
        assert result.outcomes["union: marriage without a wedding"] == 1
        assert on(result.batch, "f5", "ended_by") == [
            ("ended_by", "f5-divorce", [])
        ]
        assert on(result.batch, "f5-divorce") == []
        assert result.outcomes["union: undated divorce"] == 1

    def test_a_placed_divorce_is_a_divorce(self):
        result = adapt_gedcom(
            family("1 DIV", "2 PLAC Rekau, Hesse, Germany"), artifact=EXPORT
        )
        assert result.unsupported_tags == {}
        assert [e[0] for e in on(result.batch, "f5-divorce")] == ["location"]

    @pytest.mark.parametrize(
        "lines", [["1 DIV"], ["1 DIV N"], ["1 DIV", "2 NOTE contested"]]
    )
    def test_a_div_that_states_no_divorce_is_counted_whole(self, lines):
        # GEDCOM 5.5.1 asserts an event by a DATE, a PLAC or a Y: a DIV
        # with none says nothing of an end
        result = adapt_gedcom(family(*lines), artifact=EXPORT)
        assert result.unsupported_tags == {"DIV": 1}
        assert unions(result.batch) == {"f5": "partnership"}

    def test_a_familys_divorces_are_one_divorce(self):
        batch = adapt_gedcom(
            family("1 DIV", "2 DATE 2 JAN 1957", "1 DIV", "2 DATE 1957"),
            artifact=EXPORT,
        ).batch
        assert unions(batch) == {"f5": "marriage", "f5-divorce": "divorce"}
        assert dates(
            family("1 DIV", "2 DATE 2 JAN 1957", "1 DIV", "2 DATE 1957"),
            "f5-divorce",
        ) == [
            ("point_in_time", (1957, 1, 2, "day"), []),
            ("point_in_time", year(1957), []),
        ]

    def test_an_annulment_is_an_event_of_its_own(self):
        result = adapt_gedcom(
            family(
                "1 MARR",
                "2 DATE 8 FEB 1947",
                "1 ANUL",
                "2 DATE ABT 1948",
                "2 PLAC Rekau, Hesse, Germany",
            ),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
            "f5-annulment": "annulment",
        }
        assert on(result.batch, "f5", "ended_by") == [
            ("ended_by", "f5-annulment", [])
        ]
        assert [e[:2] for e in on(result.batch, "f5-annulment")] == [
            ("point_in_time", year(1948)),
            ("location", "pl1"),
        ]
        assert result.unsupported_tags == {}

    def test_no_end_is_inferred_from_a_partners_death(self):
        # (here) Leopold's record gives his death: it ends no union, as
        # GEDCOM does not say that the marriage lasted until then
        text = "\n".join(
            [
                "0 @I11@ INDI",
                "1 NAME Leopold /Löbig/",
                "1 DEAT",
                "2 DATE 1960",
                "0 @I17@ INDI",
                "1 NAME Agnes /Löbig/",
                "0 @F5@ FAM",
                "1 HUSB @I11@",
                "1 WIFE @I17@",
                "1 MARR",
                "2 DATE 8 FEB 1947",
                "0 TRLR",
                "",
            ]
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert on(batch, "f5", "ended_by") == []

    def test_an_undated_marriage_is_an_undated_wedding(self):
        result = adapt_gedcom(family("1 MARR Y"), artifact=EXPORT)
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
        }
        assert on(result.batch, "f5-wedding") == []
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        "lines",
        [
            ["1 MARR"],
            ["1 MARR N"],
            ["1 MARR N", "2 DATE sometime"],
            ["1 MARR", "2 NOTE the tree's first sheet"],
            ["1 MARR", "2 TYPE civil"],
            ["1 MARR", "2 DATE"],
            ["1 MARR", "2 PLAC , ,"],
        ],
    )
    def test_a_marr_that_states_no_wedding_is_counted_whole(self, lines):
        # a MARR states its wedding as a DIV states its divorce: by a Y, a
        # DATE or a PLAC; one that states none makes no wedding, and a
        # family whose MARRs state none is a partnership
        result = adapt_gedcom(family(*lines), artifact=EXPORT)
        assert unions(result.batch) == {"f5": "partnership"}
        assert on(result.batch, "f5", "started_by") == []
        assert result.unsupported_tags == {"MARR": 1}
        assert result.outcomes["union: partnership"] == 1
        assert "union: marriage" not in result.outcomes

    def test_a_marr_that_states_no_wedding_beside_one_that_does(self):
        # the stated MARR makes the marriage and its wedding; the other is
        # counted, and its line is in no claim's excerpt
        result = adapt_gedcom(
            family("1 MARR N", "1 MARR", "2 DATE 8 FEB 1947"),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
        }
        assert result.unsupported_tags == {"MARR": 1}
        for claim in result.batch.claims:
            assert "MARR N" not in claim.source.transcription_excerpt.text

    def test_an_unparsed_date_warns_and_gives_no_date(self):
        result = adapt_gedcom(
            couple("SOMEDAY MAYBE", "NEVER EVER"), artifact=EXPORT
        )
        assert result.warnings == [
            "line 9: unparsed date 'SOMEDAY MAYBE' — no point_in_time "
            "claim emitted",
            "line 11: unparsed date 'NEVER EVER' — no point_in_time "
            "claim emitted",
        ]
        batch = result.batch
        assert unions(batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
            "f5-divorce": "divorce",
        }
        assert on(batch, "f5-wedding") == []
        assert on(batch, "f5-divorce") == []

    def test_two_unions_of_one_couple_have_labels_of_their_own(self):
        # (here) the export gives Leopold and Agnes two family records: the
        # writer finds an item by its label, so each union keeps its own
        text = family("1 MARR", "2 DATE 8 FEB 1947").replace(
            "0 TRLR\n",
            "0 @F6@ FAM\n1 HUSB @I11@\n1 WIFE @I17@\n1 MARR\n"
            "2 DATE 1958\n0 TRLR\n",
        )
        labels = {
            e.id: e.label for e in adapt_gedcom(text, EXPORT).batch.entities
        }
        assert labels["f5"] == "Marriage of Leopold Löbig and Agnes Löbig"
        assert labels["f6"] == "Marriage of Leopold Löbig and Agnes Löbig (2)"
        assert labels["f6-wedding"] == (
            "Wedding of Leopold Löbig and Agnes Löbig (2)"
        )

    def test_each_union_claims_excerpt_names_the_couple(self):
        batch = adapt_gedcom(
            couple("8 FEB 1947", "2 JAN 1957"), artifact=EXPORT
        ).batch
        for claim in batch.claims:
            if claim.subject.local_id.startswith("f5"):
                excerpt = claim.source.transcription_excerpt.text
                assert excerpt.startswith(
                    "0 @F5@ FAM | 1 HUSB @I11@ | 1 WIFE @I17@"
                )
        (divorce,) = claims_for(batch, "f5-divorce", "point_in_time")
        assert divorce.source.transcription_excerpt.text == (
            "0 @F5@ FAM | 1 HUSB @I11@ | 1 WIFE @I17@ | 1 DIV | "
            "2 DATE 2 JAN 1957"
        )

    def test_every_union_batch_is_manifest_valid(self):
        batch = adapt_gedcom(
            family(
                "1 MARR",
                "2 TYPE civil",
                "2 DATE FROM 1947 TO 1957",
                "2 PLAC Flörburg, Hesse, Germany",
                "1 DIV",
                "2 DATE FROM 1956 TO 1957",
                "1 ANUL Y",
            ),
            artifact=EXPORT,
        ).batch
        assert validate_against_manifest(batch) == []

    def test_a_partners_age_at_the_wedding_is_counted_with_its_partner(
        self,
    ):
        # (here) the MARR gives Leopold's age as 32 and Agnes's as 34: a
        # HUSB or WIFE beneath a MARR goes unread, and it stands for the
        # AGE beneath it
        result = adapt_gedcom(
            family(
                "1 MARR",
                "2 DATE 8 FEB 1947",
                "2 HUSB",
                "3 AGE 32",
                "2 WIFE",
                "3 AGE 34",
            ),
            artifact=EXPORT,
        )
        assert result.unsupported_tags == {"MARR.HUSB": 1, "MARR.WIFE": 1}


def gustav(tag: str, date: str) -> str:
    """Gustav's record: his residence at 8 Hawthorn Road (RESI) or his
    occupation, hatter (OCCU), dated (here) as given."""
    event = {
        "RESI": [
            "1 RESI",
            f"2 DATE {date}",
            "2 PLAC 8 Hawthorn Road, Stupfield, Victoria, Australia",
        ],
        "OCCU": ["1 OCCU Hatter", f"2 DATE {date}"],
    }[tag]
    return "\n".join(
        ["0 @I8@ INDI", "1 NAME Gustav /Löbig/", *event, "0 TRLR", ""]
    )


# the property each event gives, which its warnings name too
HELD = {"RESI": "residence", "OCCU": "occupation"}


def held_qualifiers(tag: str, date: str) -> list[tuple]:
    """The qualifiers of Gustav's one residence or occupation claim, as
    (property, value) pairs in property order."""
    batch = adapt_gedcom(gustav(tag, date), artifact=EXPORT).batch
    (held,) = claims_for(batch, "i8", HELD[tag])
    return sorted((q.property, shown(q.value)) for q in held.qualifiers)


CIRCA = ("sourcing_circumstances", "circa")


@pytest.mark.parametrize("tag", ["RESI", "OCCU"])
class TestPeriods:
    """A residence's or an occupation's date period (FROM x TO y, FROM x,
    TO y) is its statement's start and end time, each with the range its
    own phrase states, as a divorce's date period is the divorce's own
    start and end (TestUnions); any other date is a point in time
    (docs/MODELING.md, "Marriages, residences and other periods"; #308)."""

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "FROM 1939 TO 1952",
                [("end_time", year(1952)), ("start_time", year(1939))],
            ),
            (
                "from 1939 to 1952",
                [("end_time", year(1952)), ("start_time", year(1939))],
            ),
            ("FROM 1939", [("start_time", year(1939))]),
            ("TO 1952", [("end_time", year(1952))]),
        ],
    )
    def test_a_period_is_a_start_and_an_end(self, tag, date, expected):
        # never a point in time, nor one date's range
        assert held_qualifiers(tag, date) == expected

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "FROM BEF 1940 TO 1952",
                [
                    ("end_time", year(1952)),
                    ("latest_start_date", year(1940)),
                    ("start_time", year(1940)),
                ],
            ),
            (
                "FROM AFT 1939 TO 1952",
                [
                    ("earliest_date", year(1939)),
                    ("end_time", year(1952)),
                    ("start_time", year(1939)),
                ],
            ),
            (
                "FROM BET 1939 AND 1940 TO 1952",
                [
                    ("earliest_date", year(1939)),
                    ("end_time", year(1952)),
                    ("latest_start_date", year(1940)),
                    ("start_time", year(1939)),
                ],
            ),
            (
                "FROM 1939 TO BEF 1953",
                [
                    ("end_time", year(1953)),
                    ("latest_date", year(1953)),
                    ("start_time", year(1939)),
                ],
            ),
            (
                "FROM 1939 TO AFT 1952",
                [
                    ("earliest_end_date", year(1952)),
                    ("end_time", year(1952)),
                    ("start_time", year(1939)),
                ],
            ),
            (
                "FROM 1939 TO BET 1952 AND 1953",
                [
                    ("earliest_end_date", year(1952)),
                    ("end_time", year(1952)),
                    ("latest_date", year(1953)),
                    ("start_time", year(1939)),
                ],
            ),
            (
                "FROM BET 1939 AND 1940 TO BET 1952 AND 1953",
                [
                    ("earliest_date", year(1939)),
                    ("earliest_end_date", year(1952)),
                    ("end_time", year(1952)),
                    ("latest_date", year(1953)),
                    ("latest_start_date", year(1940)),
                    ("start_time", year(1939)),
                ],
            ),
            (
                "FROM BEF 1940",
                [
                    ("latest_start_date", year(1940)),
                    ("start_time", year(1940)),
                ],
            ),
            (
                "TO AFT 1952",
                [
                    ("earliest_end_date", year(1952)),
                    ("end_time", year(1952)),
                ],
            ),
        ],
    )
    def test_each_end_takes_the_range_its_phrase_states(
        self, tag, date, expected
    ):
        # a start runs from its earliest date to its latest start date, an
        # end from its earliest end date to its latest date: no bound of
        # one end is read as the other's
        assert held_qualifiers(tag, date) == expected

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "FROM ABT 1939 TO 1952",
                [("end_time", year(1952)), CIRCA, ("start_time", year(1939))],
            ),
            (
                "FROM 1939 TO CAL 1952",
                [("end_time", year(1952)), CIRCA, ("start_time", year(1939))],
            ),
            # one circa stands for both ends
            (
                "FROM EST 1939 TO ABT 1952",
                [("end_time", year(1952)), CIRCA, ("start_time", year(1939))],
            ),
            (
                "FROM AUG 1939 TO 14 MAR 1952",
                [
                    ("end_time", (1952, 3, 14, "day")),
                    ("start_time", (1939, 8, None, "month")),
                ],
            ),
            (
                "FROM ABT AUG 1939",
                [CIRCA, ("start_time", (1939, 8, None, "month"))],
            ),
        ],
    )
    def test_circa_and_precision_are_kept_as_stated(self, tag, date, expected):
        assert held_qualifiers(tag, date) == expected

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            ("1952", [("point_in_time", year(1952))]),
            # a range bounds one date: the observation's
            (
                "BET 1939 AND 1940",
                [
                    ("earliest_date", year(1939)),
                    ("latest_date", year(1940)),
                    ("point_in_time", year(1939)),
                ],
            ),
        ],
    )
    def test_any_other_date_is_a_point_in_time(self, tag, date, expected):
        assert held_qualifiers(tag, date) == expected

    def test_a_placed_period_leaves_nothing_uncounted(self, tag):
        # (here) Gustav at 8 Hawthorn Road, or a hatter in Stupfield, from
        # 1939 to 1952: the census takes the DATE and the PLAC as read
        event, place = {
            "RESI": (
                "1 RESI",
                "8 Hawthorn Road, Stupfield, Victoria, Australia",
            ),
            "OCCU": ("1 OCCU Hatter", "Stupfield, Victoria, Australia"),
        }[tag]
        text = "\n".join(
            [
                "0 @I8@ INDI",
                "1 NAME Gustav /Löbig/",
                event,
                "2 DATE FROM 1939 TO 1952",
                f"2 PLAC {place}",
                "0 TRLR",
                "",
            ]
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}

    @pytest.mark.parametrize(
        ("date", "omitted", "expected"),
        [
            (
                "FROM SOMEDAY TO 1952",
                ["start_time"],
                [("end_time", year(1952))],
            ),
            ("FROM 1939 TO NEVER", ["end_time"], [("start_time", year(1939))]),
            ("FROM SOMEDAY TO NEVER", ["start_time", "end_time"], []),
            ("FROM", ["start_time"], []),
            ("TO", ["end_time"], []),
            ("FROM 1939 TO", ["end_time"], [("start_time", year(1939))]),
            # a period within a period is no date
            ("TO FROM 1950 TO 1952", ["end_time"], []),
            ("FROM FROM 1939 AND 1940", ["start_time"], []),
            # the grammar reads no INT date, but the "to" inside its
            # parenthesised phrase ends no FROM phrase, so the TO stands
            (
                "FROM INT 1939 (Aug to Sep) TO 1952",
                ["start_time"],
                [("end_time", year(1952))],
            ),
            # a stray ")" closes nothing: the "to" of the phrase opened
            # after it still ends no FROM phrase
            (
                "FROM 1939) (Aug to Sep) TO 1952",
                ["start_time"],
                [("end_time", year(1952))],
            ),
        ],
    )
    def test_an_unparsed_end_warns_and_is_omitted(
        self, tag, date, omitted, expected
    ):
        result = adapt_gedcom(gustav(tag, date), artifact=EXPORT)
        assert result.warnings == [
            f"line 4: unparsed {HELD[tag]} date {date!r} — {end} omitted"
            for end in omitted
        ]
        assert held_qualifiers(tag, date) == expected


@pytest.mark.parametrize(
    ("text", "subject", "prop", "expected"),
    [
        # (here) Eugen born "from 1836 to 1837"
        pytest.param(
            eugen("1 BIRT", "2 DATE FROM 1836 TO 1837"),
            "i1",
            "date_of_birth",
            [("earliest_date", year(1836)), ("latest_date", year(1837))],
            id="BIRT",
        ),
        # (here) buried "from 1909 to 1910"
        pytest.param(
            eugen(
                "1 BURI",
                "2 DATE FROM 1909 TO 1910",
                "2 PLAC Friedhof Flörburg, Hesse, Germany",
            ),
            "i1",
            "burial_place",
            [
                ("earliest_date", year(1909)),
                ("latest_date", year(1910)),
                ("point_in_time", year(1909)),
            ],
            id="BURI",
        ),
        # (here) the studio portrait dated "from 1890 to 1899"
        pytest.param(
            "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
            "1 DATE FROM 1890 TO 1899\n" + eugen("1 OBJE @O1@"),
            "i1",
            "image",
            [
                ("earliest_date", year(1890)),
                ("latest_date", year(1899)),
                ("point_in_time", year(1890)),
            ],
            id="OBJE",
        ),
    ],
)
def test_a_period_on_another_event_is_one_dates_range(
    text, subject, prop, expected
):
    """FROM x TO y on any other event keeps the reading parse_gedcom_date
    gives it, the range of one date: only a residence's, an occupation's,
    a marriage's and a divorce's period is a start and an end (#308;
    TestUnions)."""
    result = adapt_gedcom(text, artifact=EXPORT)
    assert result.warnings == []
    (claim,) = claims_for(result.batch, subject, prop)
    assert (
        sorted((q.property, shown(q.value)) for q in claim.qualifiers)
        == expected
    )


def described(value, labels: dict[str, str]):
    """A claim's or a qualifier's value: a time as (year, month, day,
    precision), an entity by its label, a vocabulary item by its key, a
    text as itself and a quantity as its amount."""
    if value.type == "time":
        return value.year, value.month, value.day, value.precision
    if value.type == "item":
        ref = value.value
        return labels[ref.local_id] if ref.kind == "local" else ref.key
    if value.type == "monolingual":
        return value.value.text
    return value.amount


def stated(text: str, subject: str, prop: str) -> list[tuple]:
    """`subject`'s `prop` claims in the batch's order, each as its value and
    its qualifiers, which are (property, value) pairs in property order."""
    batch = adapt_gedcom(text, artifact=EXPORT).batch
    labels = {entity.id: entity.label for entity in batch.entities}
    return [
        (
            described(claim.value, labels),
            sorted(
                (q.property, described(q.value, labels))
                for q in claim.qualifiers
            ),
        )
        for claim in claims_for(batch, subject, prop)
    ]


def union_events(text: str) -> dict[str, list[tuple]]:
    """Each event of the batch's unions, by id, with its claims in the
    batch's order, each as its property, its value and its qualifiers,
    which are (property, value) pairs in property order."""
    batch = adapt_gedcom(text, artifact=EXPORT).batch
    labels = {entity.id: entity.label for entity in batch.entities}
    events = [
        e.id
        for e in batch.entities
        if e.kind in UNION_KINDS - {"partnership", "marriage"}
    ]
    return {
        event: [
            (
                claim.property,
                described(claim.value, labels),
                sorted(
                    (q.property, described(q.value, labels))
                    for q in claim.qualifiers
                ),
            )
            for claim in claims_for(batch, event)
        ]
        for event in events
    }


def day(y: int, m: int, d: int) -> tuple:
    return y, m, d, "day"


FLOERBURG = "Flörburg, Hesse, Germany"


class TestRepeatedEvents:
    """A record that repeats a BIRT or a DEAT states alternatives, as
    GEDCOM does, and each is read as claims of its own, with its own date,
    place and circa, so that the alternatives coexist (ADR-0002, #322)."""

    def test_each_birth_is_a_claim_of_its_own(self):
        # Gustav was born on 7 January 1919 per the civil register and on
        # 1 July 1919 per his naturalisation: (here) the export gives both
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 BIRT",
            "2 DATE 7 JAN 1919",
            f"2 PLAC {FLOERBURG}",
            "1 BIRT",
            "2 DATE 1 JUL 1919",
        )
        assert stated(text, "i8", "date_of_birth") == [
            (day(1919, 1, 7), []),
            (day(1919, 7, 1), []),
        ]
        assert stated(text, "i8", "place_of_birth") == [(FLOERBURG, [])]
        # each cites the lines of its own BIRT
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert [
            claim.source.transcription_excerpt.text
            for claim in claims_for(batch, "i8", "date_of_birth")
        ] == [
            f"1 BIRT | 2 DATE 7 JAN 1919 | 2 PLAC {FLOERBURG}",
            "1 BIRT | 2 DATE 1 JUL 1919",
        ]

    def test_each_birth_keeps_its_own_circa(self):
        # Cäcilie was born about 1833 per the tree and in 1833 per the
        # gravestone: the circa is the tree's alone
        text = person(
            "I2",
            "Cäcilie /Tarle/",
            "1 BIRT",
            "2 DATE ABT 1833",
            "1 BIRT",
            "2 DATE 1833",
        )
        assert stated(text, "i2", "date_of_birth") == [
            (year(1833), [CIRCA]),
            (year(1833), []),
        ]

    def test_each_death_is_a_claim_of_its_own(self):
        # Eugen died on 16 March 1909 of Wassersucht, per the burial
        # register, and in 1909 per the gravestone; (here) the export's
        # second DEAT gives the cause in English
        text = eugen(
            "1 DEAT",
            "2 DATE 16 MAR 1909",
            f"2 PLAC {FLOERBURG}",
            "2 CAUS Wassersucht",
            "1 DEAT",
            "2 DATE 1909",
            "2 CAUS Dropsy",
        )
        assert stated(text, "i1", "date_of_death") == [
            (day(1909, 3, 16), []),
            (year(1909), []),
        ]
        assert stated(text, "i1", "place_of_death") == [(FLOERBURG, [])]
        assert stated(text, "i1", "cause_of_death") == [
            ("Wassersucht", []),
            ("Dropsy", []),
        ]

    @pytest.mark.parametrize(
        ("tag", "date", "prop", "expected"),
        [
            ("BIRT", "1836", "date_of_birth", year(1836)),
            ("DEAT", "16 MAR 1909", "date_of_death", day(1909, 3, 16)),
        ],
    )
    def test_an_undated_event_hides_no_dated_one(
        self, tag, date, prop, expected
    ):
        # (here) the export says first that Eugen was born, or died, and
        # then when
        text = eugen(f"1 {tag} Y", f"1 {tag}", f"2 DATE {date}")
        assert stated(text, "i1", prop) == [(expected, [])]

    def test_a_restated_event_gives_its_claims_once(self):
        # (here) the export repeats Eugen's birth line for line: the batch
        # holds each claim once, so each has a claim id of its own on the
        # review sheet
        from yizkr.pipeline.review import claim_id

        birth = [
            "1 BIRT",
            "2 DATE 1836",
            "2 PLAC Bad Flörburg, Hesse, Germany",
        ]
        batch = adapt_gedcom(eugen(*birth, *birth), artifact=EXPORT).batch
        assert len(claims_for(batch, "i1", "date_of_birth")) == 1
        assert len(claims_for(batch, "i1", "place_of_birth")) == 1
        assert len({claim_id(c) for c in batch.claims}) == len(batch.claims)

    def test_a_restated_value_under_another_citation_is_its_own_claim(self):
        # Gustav was born on 7 January 1919 per his birth register extract:
        # (here) the export gives that date twice, citing the extract once
        # and the tree once, with no PAGE, so only the citation tells the
        # two apart. Each stays a claim of its own, with its own excerpt,
        # so that neither source's reference is lost (rule 1, #322)
        text = (
            "0 @S1@ SOUR\n1 TITL Birth register extract for Gustav Löbig\n"
            "0 @S2@ SOUR\n1 TITL Family tree drawn by Clara Lobig\n"
        ) + person(
            "I8",
            "Gustav /Löbig/",
            "1 BIRT",
            "2 DATE 7 JAN 1919",
            "2 SOUR @S1@",
            "1 BIRT",
            "2 DATE 7 JAN 1919",
            "2 SOUR @S2@",
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        births = claims_for(batch, "i8", "date_of_birth")
        assert [shown(claim.value) for claim in births] == [
            day(1919, 1, 7),
            day(1919, 1, 7),
        ]
        assert [
            claim.source.transcription_excerpt.text for claim in births
        ] == [
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 SOUR @S1@"
            " | cited: Birth register extract for Gustav Löbig",
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 SOUR @S2@"
            " | cited: Family tree drawn by Clara Lobig",
        ]
        assert [claim.source.page_number for claim in births] == [None, None]

    def test_the_summary_gives_every_birth_and_death_date(self):
        # the undated BIRT hides neither birth date, and (here) the export
        # dates Gustav's death twice
        batch = adapt_gedcom(
            person(
                "I8",
                "Gustav /Löbig/",
                "1 BIRT Y",
                "1 BIRT",
                "2 DATE 7 JAN 1919",
                "1 BIRT",
                "2 DATE 1 JUL 1919",
                "1 DEAT",
                "2 DATE 1996",
                "1 DEAT",
                "2 DATE ABT 1996",
            ),
            artifact=EXPORT,
        ).batch
        (gustav,) = [e for e in batch.entities if e.id == "i8"]
        assert gustav.summary == (
            "b. 7 JAN 1919 or 1 JUL 1919; d. 1996 or ABT 1996"
        )


class TestRepeatedDetails:
    """A second DATE, PLAC or CAUS beneath one event is non-standard
    GEDCOM, and an alternative too: each is read as a claim of its own.
    Where one claim holds both an event's date and its place, each of its
    dates is paired with each of its places (#322)."""

    def test_each_date_beneath_a_birth_is_a_claim_of_its_own(self):
        # (here) one BIRT gives both of Gustav's birth dates
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 BIRT",
            "2 DATE 7 JAN 1919",
            "2 DATE 1 JUL 1919",
        )
        assert stated(text, "i8", "date_of_birth") == [
            (day(1919, 1, 7), []),
            (day(1919, 7, 1), []),
        ]

    def test_each_place_beneath_a_birth_is_a_claim_of_its_own(self):
        # Hildegard's Fläckfeld lies in Kreis Mockburg per the 1905
        # certificate and in Kreis Peiltal per the 1984 tree: (here) one
        # BIRT gives both
        mockburg = "Fläckfeld, Kreis Mockburg, Hesse, Germany"
        peiltal = "Fläckfeld, Kreis Peiltal, Hesse, Germany"
        text = person(
            "I4",
            "Hildegard /Laudler/",
            "1 BIRT",
            "2 DATE 19 MAR 1876",
            f"2 PLAC {mockburg}",
            f"2 PLAC {peiltal}",
        )
        assert stated(text, "i4", "place_of_birth") == [
            (mockburg, []),
            (peiltal, []),
        ]

    def test_each_cause_beneath_a_death_is_a_claim_of_its_own(self):
        text = eugen(
            "1 DEAT",
            "2 DATE 16 MAR 1909",
            "2 CAUS Dropsy",
            "2 CAUS Wassersucht",
        )
        assert stated(text, "i1", "cause_of_death") == [
            ("Dropsy", []),
            ("Wassersucht", []),
        ]

    def test_an_empty_place_hides_no_named_one(self):
        # (here) the export's burial gives an empty PLAC before the cemetery
        cemetery = "Friedhof Flörburg, Hesse, Germany"
        text = eugen(
            "1 BURI", "2 DATE 20 MAR 1909", "2 PLAC", f"2 PLAC {cemetery}"
        )
        assert adapt_gedcom(text, artifact=EXPORT).warnings == []
        assert stated(text, "i1", "burial_place") == [
            (cemetery, [("point_in_time", day(1909, 3, 20))]),
        ]

    @pytest.mark.parametrize(
        ("text", "subject", "prop", "expected", "named", "warnings"),
        [
            (
                eugen("1 BIRT", "2 DATE 1836", "2 PLAC , , ,"),
                "i1",
                "place_of_birth",
                [],
                [],
                [],
            ),
            (
                eugen("1 DEAT", "2 DATE 16 MAR 1909", "2 PLAC , ,"),
                "i1",
                "place_of_death",
                [],
                [],
                [],
            ),
            (
                eugen(
                    "1 BURI",
                    "2 DATE 20 MAR 1909",
                    "2 PLAC , , ,",
                    "2 PLAC Friedhof Flörburg, Hesse, Germany",
                ),
                "i1",
                "burial_place",
                [
                    (
                        "Friedhof Flörburg, Hesse, Germany",
                        [("point_in_time", day(1909, 3, 20))],
                    )
                ],
                # the cemetery, and the jurisdictions its PLAC names
                [
                    "Friedhof Flörburg, Hesse, Germany",
                    "Hesse, Germany",
                    "Germany",
                ],
                [],
            ),
            (
                person(
                    "I8", "Gustav /Löbig/", "1 RESI", "2 DATE 1952", "2 PLAC ,"
                ),
                "i8",
                "residence",
                [],
                [],
                [
                    "line 3: RESI without a PLAC that names a place — "
                    "no residence claim emitted"
                ],
            ),
            (
                family("1 MARR", "2 DATE 8 FEB 1947", "2 PLAC , , ,"),
                "f5-wedding",
                "location",
                [],
                [],
                [],
            ),
        ],
        ids=["BIRT", "DEAT", "BURI", "RESI", "MARR"],
    )
    def test_a_place_of_commas_alone_names_no_place(
        self, text, subject, prop, expected, named, warnings
    ):
        # (here) the export gives a PLAC whose jurisdictions a tool left
        # empty (", , ,"): it names no place, so it gives no place claim
        # and no place item
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == warnings
        assert stated(text, subject, prop) == expected
        assert [
            e.label for e in result.batch.entities if e.kind == "place"
        ] == named

    def test_a_burial_pairs_each_date_with_each_place(self):
        # Eugen was buried on 20 March 1909 in the Friedhof Flörburg, and
        # (here) in 1909 in Flörburg too
        cemetery = "Friedhof Flörburg, Hesse, Germany"
        text = eugen(
            "1 BURI",
            "2 DATE 20 MAR 1909",
            "2 DATE 1909",
            f"2 PLAC {cemetery}",
            f"2 PLAC {FLOERBURG}",
        )
        on_the_day = [("point_in_time", day(1909, 3, 20))]
        in_the_year = [("point_in_time", year(1909))]
        assert stated(text, "i1", "burial_place") == [
            (cemetery, on_the_day),
            (cemetery, in_the_year),
            (FLOERBURG, on_the_day),
            (FLOERBURG, in_the_year),
        ]

    def test_a_residence_pairs_each_date_with_each_place(self):
        # (here) Gustav lived at 8 Hawthorn Road, in Stupfield, from 1939
        # to 1952 and in 1952
        road = "8 Hawthorn Road, Stupfield, Victoria, Australia"
        town = "Stupfield, Victoria, Australia"
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 RESI",
            "2 DATE FROM 1939 TO 1952",
            "2 DATE 1952",
            f"2 PLAC {road}",
            f"2 PLAC {town}",
        )
        period = [("end_time", year(1952)), ("start_time", year(1939))]
        observed = [("point_in_time", year(1952))]
        assert stated(text, "i8", "residence") == [
            (road, period),
            (road, observed),
            (town, period),
            (town, observed),
        ]

    def test_an_occupation_pairs_each_date_with_each_place(self):
        # (here) Gustav was a hatter from 1939 to 1952 and in 1952, in
        # Flörburg and in Stupfield
        town = "Stupfield, Victoria, Australia"
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 OCCU Hatter",
            "2 DATE FROM 1939 TO 1952",
            "2 DATE 1952",
            f"2 PLAC {FLOERBURG}",
            f"2 PLAC {town}",
        )
        period = [("end_time", year(1952)), ("start_time", year(1939))]
        observed = [("point_in_time", year(1952))]
        assert stated(text, "i8", "occupation") == [
            ("Hatter", sorted([*period, ("location", FLOERBURG)])),
            ("Hatter", sorted([*observed, ("location", FLOERBURG)])),
            ("Hatter", sorted([*period, ("location", town)])),
            ("Hatter", sorted([*observed, ("location", town)])),
        ]

    def test_each_date_of_a_media_object_dates_a_claim_of_its_own(self):
        # the studio portrait of the 1890s, dated (here) to 1895 as well
        text = (
            "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
            "1 DATE BET 1890 AND 1899\n1 DATE 1895\n" + eugen("1 OBJE @O1@")
        )
        assert stated(text, "i1", "image") == [
            (
                "studio-portrait-1890s.jpg",
                [
                    ("earliest_date", year(1890)),
                    ("latest_date", year(1899)),
                    ("point_in_time", year(1890)),
                ],
            ),
            ("studio-portrait-1890s.jpg", [("point_in_time", year(1895))]),
        ]

    def test_an_unparsed_date_adds_no_undated_claim(self):
        # (here) the residence gives a date the grammar cannot parse before
        # one it can: the one it can dates the only claim
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 RESI",
            "2 DATE SOMEDAY MAYBE",
            "2 DATE 1952",
            "2 PLAC 8 Hawthorn Road, Stupfield, Victoria, Australia",
        )
        assert adapt_gedcom(text, artifact=EXPORT).warnings == [
            "line 4: unparsed residence date 'SOMEDAY MAYBE' — "
            "point_in_time omitted"
        ]
        assert stated(text, "i8", "residence") == [
            (
                "8 Hawthorn Road, Stupfield, Victoria, Australia",
                [("point_in_time", year(1952))],
            ),
        ]

    @pytest.mark.parametrize(
        ("tag", "date", "prop", "expected"),
        [
            ("BIRT", "1836", "date_of_birth", year(1836)),
            ("DEAT", "1909", "date_of_death", year(1909)),
        ],
    )
    def test_an_unparsed_date_hides_no_parsed_one(
        self, tag, date, prop, expected
    ):
        # (here) Eugen's birth, or his death, gives a date the grammar
        # cannot parse before one it can: the one it cannot is warned of,
        # and the one it can is still claimed
        text = eugen(f"1 {tag}", "2 DATE SOMEDAY MAYBE", f"2 DATE {date}")
        assert adapt_gedcom(text, artifact=EXPORT).warnings == [
            f"line 4: unparsed date 'SOMEDAY MAYBE' — no {prop} claim emitted"
        ]
        assert stated(text, "i1", prop) == [(expected, [])]


class TestRepeatedMarriages:
    """A family that repeats a MARR or a DIV states alternatives: the MARRs
    of one kind are one wedding and the DIVs one divorce, and each date and
    place of each is a claim of its own on that event, with its own circa
    (ADR-0021, ADR-0036 §5; #322)."""

    def test_each_marriage_and_divorce_dates_its_event_again(self):
        # Leopold and Agnes married on 8 February 1947 in Flörburg and were
        # divorced on 2 January 1957, per the judgment, and married in 1947
        # and divorced in 1957 per the tree: (here) the export gives both
        text = family(
            "1 MARR",
            "2 DATE 8 FEB 1947",
            f"2 PLAC {FLOERBURG}",
            "1 MARR",
            "2 DATE 1947",
            "1 DIV",
            "2 DATE 2 JAN 1957",
            "1 DIV",
            "2 DATE 1957",
        )
        assert union_events(text) == {
            "f5-wedding": [
                ("point_in_time", day(1947, 2, 8), []),
                ("location", FLOERBURG, []),
                ("point_in_time", year(1947), []),
            ],
            "f5-divorce": [
                ("point_in_time", day(1957, 1, 2), []),
                ("point_in_time", year(1957), []),
            ],
        }
        # every MARR and DIV is read, so the census counts none of them
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}

    def test_an_alternative_marriage_keeps_its_own_circa(self):
        # (here) the export also has them married about 1946: the circa is
        # that date's alone
        text = family(
            "1 MARR",
            "2 DATE 8 FEB 1947",
            "1 MARR",
            "2 DATE ABT 1946",
        )
        assert union_events(text) == {
            "f5-wedding": [
                ("point_in_time", day(1947, 2, 8), []),
                ("point_in_time", year(1946), [CIRCA]),
            ]
        }

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            (
                ["1 MARR Y", "1 MARR", "2 DATE 8 FEB 1947"],
                {"f5-wedding": [("point_in_time", day(1947, 2, 8), [])]},
            ),
            (
                ["1 DIV Y", "1 DIV", "2 DATE 2 JAN 1957"],
                {"f5-divorce": [("point_in_time", day(1957, 1, 2), [])]},
            ),
        ],
    )
    def test_an_undated_marriage_or_divorce_hides_no_dated_one(
        self, lines, expected
    ):
        # (here) the family record says first that Leopold and Agnes
        # married, or divorced, and then when: both give the one event,
        # which the dated one dates
        text = family(*lines)
        assert union_events(text) == expected
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}

    @pytest.mark.parametrize(
        ("tag", "date", "expected"),
        [
            (
                "MARR",
                "8 FEB 1947",
                {"f5-wedding": [("point_in_time", day(1947, 2, 8), [])]},
            ),
            (
                "DIV",
                "2 JAN 1957",
                {"f5-divorce": [("point_in_time", day(1957, 1, 2), [])]},
            ),
        ],
    )
    def test_an_unparsed_date_hides_no_parsed_one_of_the_same_event(
        self, tag, date, expected
    ):
        # (here) one MARR, or one DIV, gives a date the grammar cannot parse
        # before one it can: the one it cannot is warned of, and the one it
        # can still dates the event
        text = family(f"1 {tag}", "2 DATE SOMEDAY MAYBE", f"2 DATE {date}")
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == [
            "line 9: unparsed date 'SOMEDAY MAYBE' — no point_in_time "
            "claim emitted"
        ]
        assert result.unsupported_tags == {}
        assert union_events(text) == expected

    def test_an_undated_marriage_keeps_its_place_beside_a_dated_one(self):
        # (here) one MARR places the wedding in Flörburg and another dates it
        text = family(
            "1 MARR", f"2 PLAC {FLOERBURG}", "1 MARR", "2 DATE 8 FEB 1947"
        )
        assert union_events(text) == {
            "f5-wedding": [
                ("location", FLOERBURG, []),
                ("point_in_time", day(1947, 2, 8), []),
            ]
        }

    def test_a_restated_marriage_adds_nothing(self):
        # (here) the export gives the wedding twice, as the judgment does
        wedding = ["1 MARR", "2 DATE 8 FEB 1947", f"2 PLAC {FLOERBURG}"]
        text = family(*wedding, *wedding)
        assert union_events(text) == {
            "f5-wedding": [
                ("point_in_time", day(1947, 2, 8), []),
                ("location", FLOERBURG, []),
            ]
        }
        assert len(stated(text, "f5", "started_by")) == 1

    def test_each_date_or_place_beneath_one_marriage_is_an_alternative(self):
        # (here) one MARR gives both dates and two places, and one DIV both
        # dates: each is a claim of its own
        rekau = "Rekau, Hesse, Germany"
        text = family(
            "1 MARR",
            "2 DATE 8 FEB 1947",
            "2 DATE 1947",
            f"2 PLAC {FLOERBURG}",
            f"2 PLAC {rekau}",
            "1 DIV",
            "2 DATE 2 JAN 1957",
            "2 DATE 1957",
        )
        assert union_events(text) == {
            "f5-wedding": [
                ("point_in_time", day(1947, 2, 8), []),
                ("point_in_time", year(1947), []),
                ("location", FLOERBURG, []),
                ("location", rekau, []),
            ],
            "f5-divorce": [
                ("point_in_time", day(1957, 1, 2), []),
                ("point_in_time", year(1957), []),
            ],
        }

    @pytest.mark.parametrize("tag", ["HUSB", "WIFE"])
    def test_a_further_husband_or_wife_is_counted(self, tag):
        # a family has one husband and one wife: (here) Leopold and Agnes's
        # family record names a further one, which the census counts, and
        # no claim makes a spouse of
        text = "\n".join(
            [
                "0 @I11@ INDI",
                "1 NAME Leopold /Löbig/",
                "0 @I17@ INDI",
                "1 NAME Agnes /Löbig/",
                "0 @I99@ INDI",
                "1 NAME A /B/",
                "0 @F5@ FAM",
                "1 HUSB @I11@",
                "1 WIFE @I17@",
                f"1 {tag} @I99@",
                "1 MARR",
                "2 DATE 8 FEB 1947",
                "0 TRLR",
                "",
            ]
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {f"FAM.{tag}": 1}
        assert stated(text, "f5", "participant") == [
            ("Leopold Löbig", [("object_has_role", "spouse")]),
            ("Agnes Löbig", [("object_has_role", "spouse")]),
        ]


class TestRepeatedAttributes:
    """A SEX or an NCHI given twice, and a second TYPE or NICK beneath a
    NAME, are read as claims of their own too: two that differ coexist
    (ADR-0002). One that restates another line for line, its citation
    included, is the same claim, which the batch holds once; the same value
    under another citation is a claim of its own (#322)."""

    def test_each_sex_is_a_claim_of_its_own(self):
        # no source gives the sex of the unnamed infant of 1922: (here) the
        # export gives it as M, as F and as M again
        text = person("I13", "N.N. /Löbig/", "1 SEX M", "1 SEX F", "1 SEX M")
        assert stated(text, "i13", "sex_or_gender") == [
            ("male", []),
            ("female", []),
        ]

    def test_each_child_count_is_a_claim_of_its_own(self):
        # Eugen and Cäcilie had three children; (here) the export also counts
        # four, after a count that is no number
        text = eugen("1 NCHI several", "1 NCHI 3", "1 NCHI 4")
        assert adapt_gedcom(text, artifact=EXPORT).warnings == [
            "line 3: NCHI 'several' is not a number — no number_of_children "
            "claim emitted"
        ]
        assert stated(text, "i1", "number_of_children") == [(3, []), (4, [])]

    def test_each_type_beneath_a_name_types_it(self):
        # (here) the export types Agnes's married name as official too
        text = person(
            "I17", "Agnes /Löbig/", "2 TYPE married", "2 TYPE official"
        )
        assert stated(text, "i17", "married_name") == [("Agnes Löbig", [])]
        assert stated(text, "i17", "official_name") == [("Agnes Löbig", [])]

    def test_each_nick_beneath_a_name_is_a_nickname(self):
        # Gustav was Gustl, and later Gus: (here) one NAME gives both
        text = person(
            "I8",
            "Gustav /Löbig/",
            "2 TYPE birth",
            "2 NICK Gustl",
            "2 NICK Gus",
        )
        assert stated(text, "i8", "nickname") == [("Gustl", []), ("Gus", [])]


def cited(*titles: str) -> str:
    """Level-0 SOUR records @S1@, @S2@... titled (in turn) as given."""
    return "".join(
        f"0 @S{number}@ SOUR\n1 TITL {title}\n"
        for number, title in enumerate(titles, start=1)
    )


BURIAL_REGISTER = "Burial register of Friedhof Flörburg: Eugen Löbig"
GRAVESTONE = "Photograph of the Löbig gravestone, Bad Flörburg"
TREE = "Family tree drawn by Clara Lobig"
POSTCARD = "Postcard from Sebastian Löbig to his daughter Klara"


def death_events(batch) -> list[tuple[str, str]]:
    """The batch's death events, as (id, label)."""
    return [(e.id, e.label) for e in batch.entities if e.kind == "death"]


def participations(batch) -> list[tuple[str, str, str]]:
    """Every participant claim of a death event, as (event, participant,
    excerpt); each names its person as the death's principal."""
    deaths = {event for event, _ in death_events(batch)}
    found = [
        claim
        for claim in batch.claims
        if claim.property == "participant" and claim.subject.local_id in deaths
    ]
    assert all(roles(claim) == ["principal"] for claim in found)
    return [
        (
            claim.subject.local_id,
            claim.value.value.local_id,
            claim.source.transcription_excerpt.text,
        )
        for claim in found
    ]


class TestUndatedDeath:
    """A DEAT states the death only as GEDCOM 5.5.1 has an event asserted:
    by a DATE or a PLAC with a value, or else by a Y on its own line. One
    that states it with no date, place or cause, by its Y alone or by a
    DATE the grammar cannot parse, says that the person died and nothing
    else: a `death` event with the person as its participant (ADR-0020 §2,
    docs/MODELING.md "Living persons and privacy"; #323). That is the
    shape the possibly-living check reads as death evidence: an item that
    is an instance of `death`, with the person as its `participant`. A
    DEAT that states no death gives no claim, and the census counts it."""

    @pytest.mark.parametrize(
        ("lines", "excerpt", "counted"),
        [
            # the burial register's record, which no claim cites, is
            # counted (#324)
            (["1 DEAT Y"], "1 DEAT Y", {"SOUR": 1}),
            (["1 DEAT y"], "1 DEAT y", {"SOUR": 1}),
            # Eugen died aged 73 per the burial register: (here) the export
            # gives that age beside the Y, and the census counts it
            (
                ["1 DEAT Y", "2 AGE 73"],
                "1 DEAT Y | 2 AGE 73",
                {"AGE": 1, "SOUR": 1},
            ),
            # (here) the export cites the burial register beside the Y
            (
                ["1 DEAT Y", "2 SOUR @S1@"],
                f"1 DEAT Y | 2 SOUR @S1@ | cited: {BURIAL_REGISTER}",
                {},
            ),
        ],
        ids=["Y", "lowercase-y", "Y-and-AGE", "Y-and-SOUR"],
    )
    def test_an_undated_death_is_a_death_event(self, lines, excerpt, counted):
        # (here) the export says that Eugen died, but not when, where or
        # of what
        text = cited(BURIAL_REGISTER) + eugen(*lines)
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == []
        assert result.unsupported_tags == counted
        assert death_events(result.batch) == [
            ("i1-death", "Death of Eugen Löbig")
        ]
        # the event is the claim's subject, and Eugen its participant
        assert participations(result.batch) == [("i1-death", "i1", excerpt)]
        for prop in ("date_of_death", "place_of_death", "cause_of_death"):
            assert not claims_for(result.batch, "i1", prop)

    def test_an_undated_death_cites_its_own_citation(self):
        # (here) the export cites the burial register, at its page, for
        # Eugen's death, and gives no date, place or cause
        text = cited(BURIAL_REGISTER) + eugen(
            "1 DEAT Y", "2 SOUR @S1@", "3 PAGE Beerdigungsbuch 1909"
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (participant,) = claims_for(batch, "i1-death", "participant")
        assert participant.source.artifact == EXPORT
        assert participant.source.page_number == "Beerdigungsbuch 1909"
        assert participant.source.transcription_excerpt.text == (
            "1 DEAT Y | 2 SOUR @S1@ | 3 PAGE Beerdigungsbuch 1909"
            f" | cited: {BURIAL_REGISTER}"
        )

    @pytest.mark.parametrize(
        "lines",
        [
            ["1 DEAT Y", "1 DEAT", "2 DATE 16 MAR 1909"],
            ["1 DEAT", "2 DATE 16 MAR 1909", "1 DEAT Y"],
        ],
        ids=["undated-first", "dated-first"],
    )
    def test_an_undated_death_beside_a_dated_one_adds_its_event(self, lines):
        # (here) one DEAT says that Eugen died, and another when: each is
        # claimed, and neither hides the other
        text = eugen(*lines)
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert stated(text, "i1", "date_of_death") == [(day(1909, 3, 16), [])]
        assert participations(batch) == [("i1-death", "i1", "1 DEAT Y")]

    def test_each_undated_death_is_a_participant_claim_on_the_one_event(
        self,
    ):
        # Eugen's death per the gravestone and per the tree: (here) the
        # export states it twice, citing each, with no date, and restates
        # the second line for line
        stated_by_tree = ["1 DEAT Y", "2 SOUR @S2@"]
        text = cited(GRAVESTONE, TREE) + eugen(
            "1 DEAT Y", "2 SOUR @S1@", *stated_by_tree, *stated_by_tree
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert death_events(batch) == [("i1-death", "Death of Eugen Löbig")]
        assert participations(batch) == [
            (
                "i1-death",
                "i1",
                f"1 DEAT Y | 2 SOUR @S1@ | cited: {GRAVESTONE}",
            ),
            ("i1-death", "i1", f"1 DEAT Y | 2 SOUR @S2@ | cited: {TREE}"),
        ]

    def test_each_person_has_a_death_event_of_their_own(self):
        # (here) the export says only that Eugen and Cäcilie died, and
        # gives Eugen a second NAME, his nickname Muckel: each event takes
        # its person's label, from the first NAME
        text = "\n".join(
            [
                "0 @I1@ INDI",
                "1 NAME Eugen /Löbig/",
                "1 NAME Muckel",
                "2 TYPE aka",
                "1 DEAT Y",
                "0 @I2@ INDI",
                "1 NAME Cäcilie /Tarle/",
                "1 DEAT Y",
                "0 TRLR",
                "",
            ]
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert death_events(batch) == [
            ("i1-death", "Death of Eugen Löbig"),
            ("i2-death", "Death of Cäcilie Tarle"),
        ]
        assert participations(batch) == [
            ("i1-death", "i1", "1 DEAT Y"),
            ("i2-death", "i2", "1 DEAT Y"),
        ]

    @pytest.mark.parametrize(
        ("lines", "prop", "expected"),
        [
            (
                ["1 DEAT", "2 DATE 16 MAR 1909"],
                "date_of_death",
                [(day(1909, 3, 16), [])],
            ),
            (
                ["1 DEAT", f"2 PLAC {FLOERBURG}"],
                "place_of_death",
                [(FLOERBURG, [])],
            ),
            (
                ["1 DEAT Y", "2 CAUS Wassersucht"],
                "cause_of_death",
                [("Wassersucht", [])],
            ),
            (
                ["1 DEAT", f"2 PLAC {FLOERBURG}", "2 CAUS Wassersucht"],
                "cause_of_death",
                [("Wassersucht", [])],
            ),
            (
                ["1 DEAT", "2 DATE SOMEDAY MAYBE", "2 DATE 1909"],
                "date_of_death",
                [(year(1909), [])],
            ),
            # an N does not deny what a DATE states (GEDCOM 5.5.1)
            (["1 DEAT N", "2 DATE 1909"], "date_of_death", [(year(1909), [])]),
        ],
        ids=[
            "date",
            "place",
            "Y-and-cause",
            "place-and-cause",
            "unparsed-and-date",
            "N-and-date",
        ],
    )
    def test_a_death_that_gives_a_claim_gives_no_event(
        self, lines, prop, expected
    ):
        # a date, a place or a cause of death is death evidence of its own
        text = eugen(*lines)
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert stated(text, "i1", prop) == expected
        assert death_events(batch) == []
        assert participations(batch) == []

    def test_the_fixtures_dated_deaths_give_no_event(self):
        batch = fixture_result().batch
        assert death_events(batch) == []
        assert participations(batch) == []

    def test_a_death_with_only_an_unparsed_date_is_a_death_event(self):
        # (here) the export dates Eugen's death by a phrase the grammar
        # cannot read: the date is warned of and omitted, and the death,
        # which its DATE states, is still claimed
        text = eugen("1 DEAT", "2 DATE SOMEDAY MAYBE")
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == [
            "line 4: unparsed date 'SOMEDAY MAYBE' — no date_of_death claim "
            "emitted"
        ]
        assert participations(result.batch) == [
            ("i1-death", "i1", "1 DEAT | 2 DATE SOMEDAY MAYBE")
        ]

    @pytest.mark.parametrize(
        "lines",
        [
            ["1 DEAT"],
            ["1 DEAT", "2 SOUR @S1@", "3 PAGE Beerdigungsbuch 1909"],
            ["1 DEAT", "2 NOTE See the burial register"],
            ["1 DEAT", "2 AGE 73"],
            ["1 DEAT", "2 OBJE @O1@"],
            ["1 DEAT", "2 TYPE Todesfall"],
            ["1 DEAT", "2 RESN privacy"],
            ["1 DEAT", "2 DATE"],
            ["1 DEAT", "2 PLAC"],
            ["1 DEAT", "2 CAUS"],
            ["1 DEAT", "2 CAUS Wassersucht"],
            ["1 DEAT N"],
            ["1 DEAT N", "2 DATE SOMEDAY MAYBE"],
            ["1 DEAT N", "2 CAUS Wassersucht"],
            ["1 DEAT 16 MAR 1909"],
            ["1 DEAT 16 MAR 1909", "2 DATE SOMEDAY MAYBE"],
            ["1 DEAT Yes"],
            # a place whose jurisdictions a tool left empty names none
            ["1 DEAT", "2 PLAC , , ,"],
        ],
        ids=[
            "bare",
            "SOUR",
            "NOTE",
            "AGE",
            "OBJE",
            "TYPE",
            "RESN",
            "empty-DATE",
            "empty-PLAC",
            "empty-CAUS",
            "CAUS",
            "N",
            "N-and-unparsed-date",
            "N-and-CAUS",
            "date-on-the-line",
            "date-on-the-line-and-unparsed-date",
            "Yes",
            "commas-PLAC",
        ],
    )
    def test_a_deat_that_states_no_death_gives_no_claim(self, lines):
        # (here) the export's DEAT for Eugen has no Y, and no DATE or PLAC
        # with a value: GEDCOM 5.5.1 lets a SOUR, a NOTE and the rest be
        # attached to it without asserting the death, and an N or a date
        # written on its own line asserts nothing. Neither a death event
        # nor a cause of death is claimed, since either would mark Eugen
        # as not possibly living; the census counts the DEAT, which
        # stands for all beneath it, and the burial register's record,
        # which no claim cites (#324)
        result = adapt_gedcom(
            cited(BURIAL_REGISTER) + eugen(*lines), artifact=EXPORT
        )
        assert result.unsupported_tags == {"DEAT": 1, "SOUR": 1}
        assert result.warnings == []
        assert death_events(result.batch) == []
        assert participations(result.batch) == []
        for prop in ("date_of_death", "place_of_death", "cause_of_death"):
            assert not claims_for(result.batch, "i1", prop)

    @pytest.mark.parametrize(
        ("lines", "summary"),
        [
            (["1 DEAT N", "2 DATE SOMEDAY MAYBE"], "b. 1836"),
            (["1 DEAT 16 MAR 1909", "2 DATE SOMEDAY MAYBE"], "b. 1836"),
            # a DEAT that states the death keeps its date, as given
            (
                ["1 DEAT", "2 DATE SOMEDAY MAYBE"],
                "b. 1836; d. SOMEDAY MAYBE",
            ),
        ],
        ids=["N", "date-on-the-line", "stated"],
    )
    def test_the_summary_dates_only_a_stated_death(self, lines, summary):
        # (here) the export dates Eugen's birth, and gives a DEAT with a
        # date the grammar cannot parse: the summary, which the private
        # site shows, gives that date only when the DEAT states the death
        batch = adapt_gedcom(
            eugen("1 BIRT", "2 DATE 1836", *lines), artifact=EXPORT
        ).batch
        (eugen_entity,) = [e for e in batch.entities if e.id == "i1"]
        assert eugen_entity.summary == summary


def leopold_and_agnes(*family_lines: str) -> str:
    """Leopold's and Agnes's records, and a family record F5 of the given
    lines alone."""
    return "\n".join(
        [
            "0 @I11@ INDI",
            "1 NAME Leopold /Löbig/",
            "0 @I17@ INDI",
            "1 NAME Agnes /Löbig/",
            "0 @F5@ FAM",
            *family_lines,
            "0 TRLR",
            "",
        ]
    )


# Leopold and Agnes's wedding in Flörburg and their divorce by the
# Landgericht Rekau, as the judgment gives them
WEDDING_AND_DIVORCE = [
    "1 MARR",
    "2 TYPE Civil",
    "2 DATE 8 FEB 1947",
    f"2 PLAC {FLOERBURG}",
    "1 DIV",
    "2 DATE 2 JAN 1957",
    "2 PLAC Rekau, Hesse, Germany",
]


class TestOnePartnerFamily:
    """A family's MARR, DIV and ANUL are events of the union of its HUSB
    and its WIFE. A family with one partner or none has no union: no claim
    names the other partner, and a claim has no unknown value to name them
    with. So its MARR, DIV and ANUL are counted, each standing for its
    branch, as a DIV that states no divorce is (ADR-0021, ADR-0036; #323)."""

    @pytest.mark.parametrize(
        "partners",
        [["1 HUSB @I11@"], ["1 WIFE @I17@"], []],
        ids=["husband", "wife", "none"],
    )
    def test_its_marriage_and_divorce_are_counted(self, partners):
        # (here) the family record names Leopold alone, Agnes alone, or
        # neither of them
        result = adapt_gedcom(
            leopold_and_agnes(*partners, *WEDDING_AND_DIVORCE, "1 ANUL Y"),
            artifact=EXPORT,
        )
        assert result.unsupported_tags == {"MARR": 1, "DIV": 1, "ANUL": 1}
        assert result.warnings == []
        assert unions(result.batch) == {}
        # the wedding's place is no place of the batch
        assert FLOERBURG not in {e.label for e in result.batch.entities}

    @pytest.mark.parametrize(
        ("record", "warnings"),
        [
            # the HUSB names no INDI record at all
            ("", []),
            # the HUSB names a person with no name
            (
                "0 @I99@ INDI\n1 SEX M\n",
                [
                    "@I99@: no NAME gives a name; skipped entirely, with the "
                    "family links that name it"
                ],
            ),
        ],
        ids=["no-record", "no-name"],
    )
    def test_a_partner_the_batch_does_not_hold_is_no_partner(
        self, record, warnings
    ):
        # (here) the family record's HUSB names no one the batch holds
        text = record + leopold_and_agnes(
            "1 HUSB @I99@", "1 WIFE @I17@", "1 MARR", "2 DATE 1947"
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == warnings
        assert result.unsupported_tags == {"MARR": 1}
        assert unions(result.batch) == {}

    def test_the_one_partner_is_still_a_parent(self):
        # (here) Felix and Ida's family record names Ida alone, with
        # Leopold as her son
        text = "\n".join(
            [
                "0 @I7@ INDI",
                "1 NAME Ida /Löbig/",
                "0 @I11@ INDI",
                "1 NAME Leopold /Löbig/",
                "0 @F3@ FAM",
                "1 WIFE @I7@",
                "1 CHIL @I11@",
                "0 TRLR",
                "",
            ]
        )
        assert stated(text, "i11", "mother") == [("Ida Löbig", [])]
        assert stated(text, "i11", "father") == []
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}

    def test_a_parent_the_batch_does_not_hold_is_no_parent(self):
        # (here) the export gives Felix no name: his and Ida's family
        # record still makes Ida Rudolf's mother, and names no father
        text = "\n".join(
            [
                "0 @I6@ INDI",
                "1 SEX M",
                "0 @I7@ INDI",
                "1 NAME Ida /Löbig/",
                "0 @I12@ INDI",
                "1 NAME Rudolf /Löbig/",
                "0 @F3@ FAM",
                "1 HUSB @I6@",
                "1 WIFE @I7@",
                "1 CHIL @I12@",
                "0 TRLR",
                "",
            ]
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == [
            "@I6@: no NAME gives a name; skipped entirely, with the family "
            "links that name it"
        ]
        assert result.unsupported_tags == {}
        # Rudolf's mother is the one claim: none names Felix
        assert [
            (claim.subject.local_id, claim.property)
            for claim in result.batch.claims
        ] == [("i12", "mother")]
        assert stated(text, "i12", "mother") == [("Ida Löbig", [])]


def rudolf(*lines: str) -> str:
    """Rudolf's record, (here) of the given lines alone."""
    return "\n".join(["0 @I12@ INDI", *lines, "0 TRLR", ""])


def leopold_in_his_families(*leopold: str) -> str:
    """Felix's record and family record, with Leopold his son; Leopold's
    record, (here) of the given lines alone; and Agnes's record and the
    family record that marries her to Leopold, (here) in 1947."""
    return "\n".join(
        [
            "0 @I6@ INDI",
            "1 NAME Felix /Löbig/",
            "0 @I11@ INDI",
            *leopold,
            "0 @I17@ INDI",
            "1 NAME Agnes /Löbig/",
            "0 @F3@ FAM",
            "1 HUSB @I6@",
            "1 CHIL @I11@",
            "0 @F5@ FAM",
            "1 HUSB @I11@",
            "1 WIFE @I17@",
            "1 MARR",
            "2 DATE 1947",
            "0 TRLR",
            "",
        ]
    )


class TestNameParts:
    """A NAME without a value is read from its parts, in the order GEDCOM
    writes a name, as the census takes them as read, provided a given name
    or a surname is among them; a NAME with a value is labelled by the
    value, and its parts reach its mention record (TestMentionRecords).
    A NAME whose value and parts give
    no name gives none, and a person none of whose NAMEs gives one is
    skipped, with a warning, and so are the family links that name them
    (#323)."""

    @pytest.mark.parametrize(
        ("lines", "label"),
        [
            (["1 NAME", "2 GIVN Rudolf", "2 SURN Löbig"], "Rudolf Löbig"),
            (["1 NAME //", "2 GIVN Rudolf", "2 SURN Löbig"], "Rudolf Löbig"),
            (["1 NAME", "2 GIVN Rudolf"], "Rudolf"),
            (["1 NAME", "2 SURN Löbig"], "Löbig"),
            # each part in the order GEDCOM writes a name, whatever the
            # order of its lines, each line of a part in turn, and each of
            # its comma-separated pieces in turn
            (
                [
                    "1 NAME",
                    "2 NSFX e",
                    "2 SURN d",
                    "2 SPFX c1, c2",
                    "2 GIVN b1,b2",
                    "2 NPFX a",
                    "2 GIVN b3",
                ],
                "a b1 b2 b3 c1 c2 d e",
            ),
        ],
        ids=["given-and-surname", "slashes", "given", "surname", "every-part"],
    )
    def test_a_name_without_a_value_is_read_from_its_parts(self, lines, label):
        # (here) the export gives Rudolf's name by its parts alone
        result = adapt_gedcom(rudolf(*lines), artifact=EXPORT)
        assert [e.label for e in result.batch.entities] == [label]
        assert result.warnings == []
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        ("lines", "label"),
        [
            # (here) Rudolf's NAME gives his given name as Rudi in its GIVN
            (
                [
                    "0 @I12@ INDI",
                    "1 NAME Rudolf /Löbig/",
                    "2 GIVN Rudi",
                    "2 SURN Löbig",
                ],
                "Rudolf Löbig",
            ),
            # Fräulein Klara Löbig, as the postcard addresses her: (here)
            # her NAME gives the Fräulein as its prefix
            (
                [
                    "0 @I9@ INDI",
                    "1 NAME Klara /Löbig/",
                    "2 NPFX Fräulein",
                    "2 GIVN Klara",
                    "2 SURN Löbig",
                ],
                "Klara Löbig",
            ),
        ],
        ids=["GIVN", "NPFX"],
    )
    def test_the_value_wins_over_the_parts(self, lines, label):
        # a NAME's parts are taken as restating its value, which is read
        result = adapt_gedcom(
            "\n".join([*lines, "0 TRLR", ""]), artifact=EXPORT
        )
        assert [e.label for e in result.batch.entities] == [label]
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        ("xref", "parts"),
        [
            ("I16", ["2 NSFX Wwe."]),
            ("I9", ["2 NPFX Fräulein"]),
            ("I9", ["2 NPFX Fräulein", "2 GIVN ,"]),
            ("I16", ["2 SPFX de, la"]),
        ],
        ids=["suffix", "prefix", "prefix-and-commas", "surname-prefix"],
    )
    def test_a_prefix_or_a_suffix_alone_gives_no_name(self, xref, parts):
        # the directory's widow, Löbig, Ursula, Wwe., and the postcard's
        # Fräulein Klara Löbig: (here) the export's one NAME for either
        # gives its suffix, its prefix or (here) a surname prefix, and no
        # given name or surname. That names no one, and as a label it
        # would make everyone so named one item, so the person is
        # skipped, with the warning
        text = "\n".join([f"0 @{xref}@ INDI", "1 NAME", *parts, "0 TRLR", ""])
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.batch.entities == []
        assert result.warnings == [
            f"@{xref}@: no NAME gives a name; skipped entirely, with the "
            "family links that name it"
        ]

    def test_a_name_read_from_its_parts_is_typed_and_nicknamed(self):
        # Gustav, naturalised as Gustave Lobig and called Gus: (here) the
        # export gives that name by its parts alone
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 NAME",
            "2 GIVN Gustave",
            "2 SURN Lobig",
            "2 TYPE immigrant",
            "2 NICK Gus",
        )
        assert stated(text, "i8", "official_name") == [("Gustave Lobig", [])]
        assert stated(text, "i8", "nickname") == [("Gus", [])]
        (gustav,) = adapt_gedcom(text, artifact=EXPORT).batch.entities
        assert (gustav.label, gustav.aliases) == (
            "Gustav Löbig",
            ["Gustave Lobig", "Gus"],
        )

    def test_a_person_named_by_parts_keeps_their_family_links(self):
        # (here) the export names Leopold by his NAME's parts alone: he is
        # Felix's son and Agnes's husband as ever
        text = leopold_in_his_families(
            "1 NAME", "2 GIVN Leopold", "2 SURN Löbig"
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == []
        assert result.unsupported_tags == {}
        assert stated(text, "i11", "father") == [("Felix Löbig", [])]
        assert stated(text, "f5", "participant") == [
            ("Leopold Löbig", [("object_has_role", "spouse")]),
            ("Agnes Löbig", [("object_has_role", "spouse")]),
        ]

    @pytest.mark.parametrize(
        "lines",
        [[], ["1 NAME //"], ["1 NAME", "2 TYPE birth"]],
        ids=["no-NAME", "slashes", "type-only"],
    )
    def test_a_person_without_a_name_is_skipped_with_their_links(self, lines):
        # (here) the export gives Leopold no name: his record, and his
        # families' claims about him, are skipped with a warning, and the
        # rest is adapted
        result = adapt_gedcom(leopold_in_his_families(*lines), artifact=EXPORT)
        assert result.warnings == [
            "@I11@: no NAME gives a name; skipped entirely, with the family "
            "links that name it"
        ]
        assert [e.id for e in result.batch.entities] == ["i6", "i17"]
        assert not [
            c
            for c in result.batch.claims
            if c.property in ("father", "mother", "participant")
        ]
        # the family has one partner the batch holds, and so no union: its
        # MARR is counted
        assert result.unsupported_tags == {"MARR": 1}

    def test_a_name_that_gives_no_name_types_nothing_but_keeps_its_nick(
        self,
    ):
        # (here) Rudolf's second NAME gives no name, only a type and his
        # nickname Rudi, and a married name gives none either
        text = person(
            "I12",
            "Rudolf /Löbig/",
            "1 NAME",
            "2 TYPE birth",
            "2 NICK Rudi",
            "1 NAME //",
            "2 TYPE aka",
            "1 _MARNM //",
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == []
        assert stated(text, "i12", "nickname") == [("Rudi", [])]
        for prop in ("birth_name", "married_name", "official_name"):
            assert stated(text, "i12", prop) == []
        (rudolf_entity,) = result.batch.entities
        assert rudolf_entity.aliases == ["Rudi"]


def record_of(text: str, entity_id: str):
    """The mention record of one entity of the batch, and the outcomes."""
    result = adapt_gedcom(text, artifact=EXPORT)
    (entity,) = [e for e in result.batch.entities if e.id == entity_id]
    return entity, result.outcomes


def parts(structure) -> dict:
    """A person's mention record as plain lists, empty fields left out."""
    found = {
        "given": [g.written for g in structure.given],
        "surnames": [(s.written, s.type) for s in structure.surnames],
        "nicknames": structure.nicknames,
        "titles": structure.titles,
        "suffixes": structure.suffixes,
        "unsplit": structure.unsplit,
    }
    return {key: value for key, value in found.items() if value}


class TestMentionRecords:
    """Each person's and place's entity carries the mention record its tags
    give (ADR-0035 §6): the adapter reads them and guesses nothing, and no
    NAME part is dropped without a count (#352)."""

    def test_every_record_of_the_fixture_stands(self):
        batch = fixture_result().batch
        for entity in batch.entities:
            if entity.kind in ("human", "place"):
                assert entity.structure is not None, entity.id
                assert structure_problems(entity) == [], entity.id

    def test_a_name_gives_its_given_names_surname_and_nickname(self):
        eugen_entity = next(
            e for e in fixture_result().batch.entities if e.id == "i1"
        )
        assert parts(eugen_entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "birth")],
            "nicknames": ["Muckel"],
        }
        # an export's given name is its own standard form
        assert [g.standard for g in eugen_entity.structure.given] == ["Eugen"]

    def test_each_names_surname_is_typed_by_its_type(self):
        caecilie = next(
            e for e in fixture_result().batch.entities if e.id == "i2"
        )
        assert parts(caecilie.structure)["surnames"] == [
            ("Tarle", "birth"),
            ("Löbig", "married"),
        ]

    def test_a_prefix_the_value_starts_with_is_a_title(self):
        # Fräulein Klara Löbig, as the postcard addresses her, and the
        # directory's Ursula Löbig, Wwe.
        klara, _ = record_of(
            person("I9", "Fräulein Klara /Löbig/", "2 NPFX Fräulein"), "i9"
        )
        assert parts(klara.structure) == {
            "given": ["Klara"],
            "surnames": [("Löbig", "other")],
            "titles": ["Fräulein"],
        }
        ursula, _ = record_of(person("I16", "Ursula /Löbig/ Wwe."), "i16")
        assert parts(ursula.structure) == {
            "given": ["Ursula"],
            "surnames": [("Löbig", "other")],
            "suffixes": ["Wwe."],
        }

    def test_a_suffix_part_gives_the_suffixes(self):
        ursula, _ = record_of(
            person("I16", "Ursula /Löbig/ Wwe.", "2 NSFX Wwe."), "i16"
        )
        assert parts(ursula.structure)["suffixes"] == ["Wwe."]

    @pytest.mark.parametrize(
        ("lines", "expected", "alias"),
        [
            # (here) Rudolf's NAME gives his given name as Rudi in its GIVN
            (
                ["2 GIVN Rudi", "2 SURN Löbig"],
                {
                    "given": ["Rudolf", "Rudi"],
                    "surnames": [("Löbig", "other")],
                },
                "Rudi Löbig",
            ),
            # (here) a prefix his value does not restate
            (
                ["2 NPFX Schüler", "2 GIVN Rudolf", "2 SURN Löbig"],
                {
                    "given": ["Rudolf"],
                    "surnames": [("Löbig", "other")],
                    "titles": ["Schüler"],
                },
                "Schüler Rudolf Löbig",
            ),
            # (here) a second surname his value does not restate
            (
                ["2 SURN Löbig, Sörne"],
                {
                    "given": ["Rudolf"],
                    "surnames": [("Löbig", "other"), ("Sörne", "other")],
                },
                "Löbig Sörne",
            ),
        ],
        ids=["given", "prefix", "surname"],
    )
    def test_a_part_the_value_does_not_restate_is_kept_and_counted(
        self, lines, expected, alias
    ):
        rudolf_entity, outcomes = record_of(
            person("I12", "Rudolf /Löbig/", *lines), "i12"
        )
        assert rudolf_entity.label == "Rudolf Löbig"
        assert parts(rudolf_entity.structure) == expected
        # the name the parts give is a name form of his, so the record
        # divides his names and adds none
        assert alias in rudolf_entity.aliases
        assert structure_problems(rudolf_entity) == []
        assert outcomes["NAME parts not restated"] == 1

    @pytest.mark.parametrize(
        ("parts_lines", "counted"),
        [(["2 NPFX Fräulein"], 1), (["2 NPFX Fräulein", "2 NSFX Wwe."], 2)],
    )
    def test_the_parts_of_a_name_that_gives_none_are_counted(
        self, parts_lines, counted
    ):
        # (here) Klara's second NAME has no value, and its parts give a
        # prefix, or a prefix and a suffix, and no name: they are counted,
        # and reach no field
        klara, outcomes = record_of(
            person("I9", "Klara /Löbig/", "1 NAME", *parts_lines), "i9"
        )
        assert parts(klara.structure) == {
            "given": ["Klara"],
            "surnames": [("Löbig", "other")],
        }
        assert outcomes["NAME part without a name"] == counted

    def test_a_particle_stays_in_its_surname(self):
        entity, _ = record_of(
            person("I1", "A /de B/", "2 SPFX de", "2 SURN B"), "i1"
        )
        assert parts(entity.structure)["surnames"] == [("de B", "other")]

    def test_a_particle_with_no_surname_is_counted(self):
        entity, outcomes = record_of(person("I1", "A /B/", "2 SPFX de"), "i1")
        assert parts(entity.structure)["surnames"] == [("B", "other")]
        assert outcomes["SPFX without a surname"] == 1

    def test_a_name_without_slashes_is_unsplit_and_untyped(self):
        # (here) the export gives Ida a second NAME, "Peppi", typed aka,
        # with no slashes: the export does not say which part is which
        text = person("I7", "Ida /Sörne/", "1 NAME Peppi", "2 TYPE aka")
        ida, outcomes = record_of(text, "i7")
        assert parts(ida.structure) == {
            "given": ["Ida"],
            "surnames": [("Sörne", "other")],
            "unsplit": ["Peppi"],
        }
        assert "Peppi" in ida.aliases
        assert stated(text, "i7", "nickname") == []
        assert outcomes["NAME without slashes"] == 1

    def test_a_name_without_slashes_whose_parts_say_which_is_which(self):
        text = person(
            "I8",
            "Gustav /Löbig/",
            "1 NAME Gustave Lobig",
            "2 GIVN Gustave",
            "2 SURN Lobig",
            "2 TYPE immigrant",
        )
        gustav_entity, outcomes = record_of(text, "i8")
        assert parts(gustav_entity.structure) == {
            "given": ["Gustav", "Gustave"],
            "surnames": [("Löbig", "other"), ("Lobig", "other")],
        }
        assert stated(text, "i8", "official_name") == [("Gustave Lobig", [])]
        assert "NAME without slashes" not in outcomes

    @pytest.mark.parametrize("kind", ["aka", "nick", "nickname"])
    def test_an_aka_names_given_names_are_nicknames(self, kind):
        text = person("I4", "Hildegard /Laudler/", "1 NAME Hilde /Löbig/",
                      f"2 TYPE {kind}")  # fmt: skip
        hildegard, _ = record_of(text, "i4")
        assert parts(hildegard.structure) == {
            "given": ["Hildegard"],
            "surnames": [("Laudler", "other"), ("Löbig", "other")],
            "nicknames": ["Hilde"],
        }
        assert stated(text, "i4", "nickname") == [("Hilde Löbig", [])]

    @pytest.mark.parametrize("kind", ["married", "marriage"])
    def test_a_married_names_surname_is_married(self, kind):
        text = person(
            "I4",
            "Hildegard /Laudler/",
            "2 TYPE maiden",
            "1 NAME Hildegard /Löbig/",
            f"2 TYPE {kind}",
        )
        hildegard, _ = record_of(text, "i4")
        assert parts(hildegard.structure)["surnames"] == [
            ("Laudler", "birth"),
            ("Löbig", "married"),
        ]

    def test_a_married_name_tag_gives_a_married_surname(self):
        ida = next(e for e in fixture_result().batch.entities if e.id == "i7")
        assert ("Löbig", "married") in parts(ida.structure)["surnames"]
        assert "Ida Löbig" in ida.aliases

    def test_a_married_name_tag_without_slashes_is_unsplit(self):
        text = person("I7", "Ida /Sörne/", "1 _MARNM Ida Löbig")
        ida, outcomes = record_of(text, "i7")
        assert parts(ida.structure)["unsplit"] == ["Ida Löbig"]
        assert outcomes["_MARNM without slashes"] == 1
        # an unsplit form is typed as nothing, as a NAME without slashes is
        assert stated(text, "i7", "married_name") == []

    def test_a_bracketed_note_is_an_alias_and_an_unsplit_form(self):
        # (here) the export notes Hildegard's maiden name in brackets
        text = person("I4", "Hildegard (geb. Laudler) /Löbig/")
        hildegard, outcomes = record_of(text, "i4")
        assert hildegard.label == "Hildegard Löbig"
        assert hildegard.aliases == ["geb. Laudler"]
        assert parts(hildegard.structure) == {
            "given": ["Hildegard"],
            "surnames": [("Löbig", "other")],
            "unsplit": ["geb. Laudler"],
        }
        assert outcomes["NAME bracketed part"] == 1
        for prop in ("nickname", "birth_name"):
            assert stated(text, "i4", prop) == []

    def test_a_form_that_alone_carries_a_part_stays_an_alias(self):
        # the alias hygiene drops a form whose match tokens the label
        # holds; a form that carries a part of the record no other form
        # holds stays, however short the part
        entity, _ = record_of(
            person("I1", "Abc /Bcd/", "1 NAME Xy /Bcd/"), "i1"
        )
        assert entity.aliases == ["Xy Bcd"]
        assert structure_problems(entity) == []

    def test_a_record_that_adds_a_name_is_a_bug_the_adapter_refuses(
        self, monkeypatch
    ):
        # deterministic code whose record adds a part no name form holds
        # is a bug, refused before any batch leaves the adapter (§5)
        from yizkr.pipeline import gedcom

        real = gedcom.read_name

        def adding(name):
            reading = real(name)
            reading.given.append("Zyx")
            return reading

        monkeypatch.setattr(gedcom, "read_name", adding)
        with pytest.raises(ValueError, match="addition: 'Zyx'"):
            adapt_gedcom(person("I1", "Eugen /Löbig/"), artifact=EXPORT)

    def test_a_place_is_named_by_its_first_component(self):
        batch = fixture_result().batch
        (place,) = [
            e
            for e in batch.entities
            if e.label == "Fläckfeld, Kreis Mockburg, Hesse, Germany"
        ]
        assert [n.written for n in place.structure.names] == ["Fläckfeld"]
        assert place.structure.containers() == [
            "Kreis Mockburg",
            "Hesse",
            "Germany",
        ]


class TestLanguage:
    """HEAD.LANG, a GEDCOM 5.5.1 language name mapped to BCP 47, is the
    language of the adapter's texts; with none the map knows, they are
    undetermined (`und`), counted (ADR-0035 §6)."""

    def languages(self, text: str) -> set[str]:
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        found = {c.source.transcription_excerpt.language for c in batch.claims}
        found |= {
            c.value.value.language
            for c in batch.claims
            if c.value.type == "monolingual"
        }
        return found

    def test_the_fixture_is_in_english(self):
        result = fixture_result()
        assert self.languages(FIXTURE.read_text()) == {"en"}
        assert not [k for k in result.outcomes if k.startswith("HEAD.LANG")]

    @pytest.mark.parametrize(
        ("name", "code"),
        [("German", "de"), ("Russian", "ru"), ("dutch", "nl"),
         ("Yiddish", "yi"), ("Hebrew", "he"), ("Polish", "pl"),
         ("de", "de"), ("RU", "ru"), (" yi ", "yi")],
    )  # fmt: skip
    def test_a_stated_language_is_mapped(self, name, code):
        text = f"0 HEAD\n1 LANG {name}\n" + eugen(
            "2 NICK Muckel", "1 DEAT", "2 DATE 1909", "2 CAUS Dropsy"
        )
        assert self.languages(text) == {code}
        assert adapt_gedcom(text, artifact=EXPORT).outcomes == {}

    @pytest.mark.parametrize(
        ("head", "outcome"),
        [
            ("0 HEAD\n", "HEAD.LANG absent: und"),
            ("", "HEAD.LANG absent: und"),
            ("0 HEAD\n1 LANG Pahari\n", "HEAD.LANG unmapped: und"),
            ("0 HEAD\n1 LANG Frisian\n", "HEAD.LANG unmapped: und"),
            # a code the map does not give is not taken as it is
            ("0 HEAD\n1 LANG fy\n", "HEAD.LANG unmapped: und"),
        ],
    )
    def test_an_absent_or_unmapped_language_is_undetermined(
        self, head, outcome
    ):
        text = head + eugen("2 NICK Muckel", "1 BIRT", "2 DATE 1836")
        assert self.languages(text) == {"und"}
        assert adapt_gedcom(text, artifact=EXPORT).outcomes == {outcome: 1}

    def test_every_mapped_code_is_a_language_a_text_takes(self):
        for code in {*GEDCOM_LANGUAGES.values(), UNDETERMINED}:
            MonolingualText(text="x", language=code)


class TestRepeatedNicknames:
    """A nickname that several NAMEs give is a claim of each, citing its
    own NAME, as any value that several records give is, so that no
    citation is lost (rule 1, #323); one NAME that gives it twice gives
    it once."""

    @pytest.mark.parametrize(
        "second",
        [
            ["1 NAME Gustav 'Gustl' /Löbig/", "2 SOUR @S2@"],
            ["1 NAME Gustav /Löbig/", "2 NICK Gustl", "2 SOUR @S2@"],
        ],
        ids=["inline", "NICK"],
    )
    def test_a_nickname_several_names_give_is_a_claim_of_each(self, second):
        # Gustav was Gustl per Sebastian's postcard of 1938 and per the
        # tree Klara drew: (here) the export gives the nickname in two
        # NAMEs, each citing one of them
        text = cited(POSTCARD, TREE) + person(
            "I8", "Gustav /Löbig/", "2 NICK Gustl", "2 SOUR @S1@", *second
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        nicknames = claims_for(batch, "i8", "nickname")
        assert [claim.value.value.text for claim in nicknames] == [
            "Gustl",
            "Gustl",
        ]
        assert [
            claim.source.transcription_excerpt.text.rsplit(" | cited: ")[-1]
            for claim in nicknames
        ] == [POSTCARD, TREE]

    def test_one_name_that_gives_a_nickname_twice_gives_one_claim(self):
        # (here) Ida's NAME gives Peppi in quotes and by a NICK
        text = person("I7", "Ida 'Peppi' /Sörne/", "2 NICK Peppi")
        assert stated(text, "i7", "nickname") == [("Peppi", [])]

    def test_a_bracket_beside_a_nick_gives_only_the_nicks_claim(self):
        # a bracket is no nickname (ADR-0035 §6), so the NICK alone gives
        # the claim
        text = person("I7", "Ida (Peppi) /Sörne/", "2 NICK Peppi")
        assert stated(text, "i7", "nickname") == [("Peppi", [])]


JUDGMENT = (
    "Judgment of the Landgericht Rekau divorcing Leopold and Agnes Löbig"
)
COMPENSATION = "Compensation file of the Wiedergutmachungsamt Peiltal for Ida"
BIRTH_EXTRACT = "Birth register extract for Gustav Löbig"
# Leopold and Agnes's wedding in Flörburg, as the judgment gives it
WEDDING = ["1 MARR", "2 DATE 8 FEB 1947", f"2 PLAC {FLOERBURG}"]
# the lines of Leopold and Agnes's family record that name them
PARTNERS = "0 @F5@ FAM | 1 HUSB @I11@ | 1 WIFE @I17@"
# the studio portrait of the 1890s, as the fixture gives it, and its back
STUDIO = "Studio portrait of the Löbig family"
STUDIO_BACK = "Back of the studio portrait, the sitters' names pencilled"
PORTRAIT = (
    "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n2 FORM jpg\n"
    "2 TITL Studio portrait of the Löbig family\n"
)
# (here) a scan of the burial register's sheet 12, as its own object
SHEET_SCAN = "0 @O2@ OBJE\n1 FILE burial-register-1909-sheet-12.jpg\n"
PORTRAIT_LINES = (
    "0 @O1@ OBJE | 1 FILE studio-portrait-1890s.jpg | 2 FORM jpg"
    " | 2 TITL Studio portrait of the Löbig family"
)


def text_of(length: int, ending: str = "") -> str:
    """A text of exactly `length` characters on Eugen's burial register
    entry, ending (here) with `ending`, and never with a space, which a
    GEDCOM line would lose."""
    sentence = (
        "Burial register of Friedhof Flörburg, 1909, sheet 12: Eugen Löbig, "
        "master hatter, of Brunnenring 35, buried on 20 March 1909. "
    )
    text = (sentence * (length // len(sentence) + 1))[: length - len(ending)]
    text = (text[:-1] + ".") if text.endswith(" ") else text
    return text + ending


def sources(text: str, subject: str, prop: str) -> list[tuple]:
    """The references of `subject`'s `prop` claims, as (page number,
    excerpt), in the batch's order."""
    batch = adapt_gedcom(text, artifact=EXPORT).batch
    return [
        (claim.source.page_number, claim.source.transcription_excerpt.text)
        for claim in claims_for(batch, subject, prop)
    ]


def ida_and_felix(*family_lines: str) -> str:
    """Felix's, Ida's and Leopold's records, and Felix and Ida's family
    record F3, with Leopold their son and (here) the given lines."""
    return "\n".join(
        [
            "0 @I6@ INDI",
            "1 NAME Felix /Löbig/",
            "0 @I7@ INDI",
            "1 NAME Ida /Löbig/",
            "0 @I11@ INDI",
            "1 NAME Leopold /Löbig/",
            "0 @F3@ FAM",
            "1 HUSB @I6@",
            "1 WIFE @I7@",
            "1 CHIL @I11@",
            *family_lines,
            "0 TRLR",
            "",
        ]
    )


class TestMarriageCitations:
    """A SOUR beneath a MARR, a DIV or an ANUL is folded into the reference
    of that event's claims, and of the union's claim that points to it; a
    SOUR of the family record, into its couple's claims. Each claim holds,
    of the family record, its own line, its HUSB and WIFE, and the lines
    of the record that gives it (ADR-0021, ADR-0036 §5; #324)."""

    def test_a_marriage_citation_is_folded_into_its_weddings_claims(self):
        # Leopold and Agnes married on 8 February 1947 in Flörburg, per the
        # judgment: (here) the export cites its second sheet beneath the
        # MARR
        text = cited(JUDGMENT) + family(
            *WEDDING, "2 SOUR @S1@", "3 PAGE Bl. 2"
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}
        wedding = (
            "Bl. 2",
            f"{PARTNERS} | 1 MARR | 2 DATE 8 FEB 1947 | 2 PLAC {FLOERBURG}"
            f" | 2 SOUR @S1@ | 3 PAGE Bl. 2 | cited: {JUDGMENT}",
        )
        assert sources(text, "f5", "started_by") == [wedding]
        assert sources(text, "f5-wedding", "point_in_time") == [wedding]
        assert sources(text, "f5-wedding", "location") == [wedding]
        # the couple's claims cite the family record, which cites nothing
        assert sources(text, "f5", "participant") == [
            (None, PARTNERS),
            (None, PARTNERS),
        ]

    def test_the_familys_and_the_divorces_citations_are_folded_in_too(self):
        # (here) the family record cites the tree, and its DIV the
        # judgment's first sheet
        text = cited(TREE, JUDGMENT) + family(
            "1 SOUR @S1@",
            *WEDDING,
            "1 DIV",
            "2 DATE 2 JAN 1957",
            "2 SOUR @S2@",
            "3 PAGE Bl. 1",
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}
        couple_source = (None, f"{PARTNERS} | 1 SOUR @S1@ | cited: {TREE}")
        assert sources(text, "f5", "participant") == [couple_source] * 2
        divorce = (
            "Bl. 1",
            f"{PARTNERS} | 1 DIV | 2 DATE 2 JAN 1957 | 2 SOUR @S2@"
            f" | 3 PAGE Bl. 1 | cited: {JUDGMENT}",
        )
        assert sources(text, "f5", "ended_by") == [divorce]
        assert sources(text, "f5-divorce", "point_in_time") == [divorce]
        # the wedding's claims cite neither
        assert sources(text, "f5-wedding", "point_in_time") == [
            (
                None,
                f"{PARTNERS} | 1 MARR | 2 DATE 8 FEB 1947"
                f" | 2 PLAC {FLOERBURG}",
            )
        ]

    def test_an_annulments_citation_is_folded_into_its_claims(self):
        text = cited(JUDGMENT) + family(
            "1 ANUL", "2 DATE 1948", "2 SOUR @S1@", "3 PAGE Bl. 1"
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}
        assert [page for page, _ in sources(text, "f5", "ended_by")] == [
            "Bl. 1"
        ]
        assert [
            page for page, _ in sources(text, "f5-annulment", "point_in_time")
        ] == ["Bl. 1"]

    def test_each_marriage_holds_its_own_lines_and_citations(self):
        # Leopold and Agnes married on 8 February 1947 per the judgment,
        # and in 1947 per the tree, and were divorced on 2 January 1957:
        # (here) the export gives both weddings, each citing its source,
        # and a NOTE on the family
        text = cited(JUDGMENT, TREE) + family(
            *WEDDING,
            "2 SOUR @S1@",
            "3 PAGE Bl. 2",
            "1 MARR",
            "2 DATE 1947",
            "2 SOUR @S2@",
            "1 DIV",
            "2 DATE 2 JAN 1957",
            "1 NOTE Divorced by the Landgericht Rekau",
        )
        # neither holds the other's marriage, nor the family's NOTE
        assert sources(text, "f5-wedding", "point_in_time") == [
            (
                "Bl. 2",
                f"{PARTNERS} | 1 MARR | 2 DATE 8 FEB 1947 | 2 PLAC {FLOERBURG}"
                f" | 2 SOUR @S1@ | 3 PAGE Bl. 2 | cited: {JUDGMENT}",
            ),
            (
                None,
                f"{PARTNERS} | 1 MARR | 2 DATE 1947 | 2 SOUR @S2@"
                f" | cited: {TREE}",
            ),
        ]
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {
            "NOTE": 1
        }

    @pytest.mark.parametrize(
        "citations",
        [
            # the judgment and the tree, each by its record
            ["2 SOUR @S1@", "2 SOUR @S2@"],
            # each named in the citation itself, with no record to resolve
            # to a title
            [f"2 SOUR {JUDGMENT}", f"2 SOUR {TREE}"],
        ],
        ids=["records", "inline"],
    )
    def test_a_marriage_restated_under_another_citation_is_its_own_claim(
        self, citations
    ):
        # Leopold and Agnes married on 8 February 1947 in Flörburg, per the
        # judgment and per the tree: (here) the export gives the wedding
        # twice, each citing one of them, with no PAGE, so only the
        # citation tells the two apart. Neither is lost (rule 1, #350)
        first, second = citations
        text = cited(JUDGMENT, TREE) + family(
            *WEDDING, first, *WEDDING, second
        )
        assert [
            value for value, _ in stated(text, "f5-wedding", "point_in_time")
        ] == [day(1947, 2, 8)] * 2
        excerpts = [
            excerpt for _, excerpt in sources(text, "f5-wedding", "location")
        ]
        assert [first in excerpts[0], second in excerpts[1]] == [True, True]
        assert [first in excerpts[1], second in excerpts[0]] == [False, False]

    def test_a_marriage_restated_at_another_page_is_its_own_claim(self):
        # (here) the export gives the wedding twice, each citing the
        # judgment, at its second sheet and at its third: the same source,
        # but not the same citation, line for line
        text = cited(JUDGMENT) + family(
            *WEDDING, "2 SOUR @S1@", "3 PAGE Bl. 2",
            *WEDDING, "2 SOUR @S1@", "3 PAGE Bl. 3",
        )  # fmt: skip
        assert [
            page for page, _ in sources(text, "f5-wedding", "point_in_time")
        ] == ["Bl. 2", "Bl. 3"]

    def test_a_marriage_restated_under_the_same_citation_adds_no_claim(self):
        # (here) the export gives the wedding twice, line for line, each
        # citing the judgment's second sheet: one claim of each
        wedding = [*WEDDING, "2 SOUR @S1@", "3 PAGE Bl. 2"]
        text = cited(JUDGMENT) + family(*wedding, *wedding)
        lines = " | ".join(wedding)
        once = [("Bl. 2", f"{PARTNERS} | {lines} | cited: {JUDGMENT}")]
        assert sources(text, "f5", "started_by") == once
        assert sources(text, "f5-wedding", "point_in_time") == once

    def test_an_undated_marriages_citation_is_folded_into_its_wedding(self):
        # (here) the family record says that Leopold and Agnes married,
        # citing the tree's second sheet, and not when or where
        marriage = "1 MARR Y"
        text = cited(TREE) + family(marriage, "2 SOUR @S1@", "3 PAGE sheet 2")
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {}
        assert sources(text, "f5", "started_by") == [
            (
                "sheet 2",
                f"{PARTNERS} | {marriage} | 2 SOUR @S1@ | 3 PAGE sheet 2"
                f" | cited: {TREE}",
            )
        ]

    def test_an_undated_marriages_citation_beside_a_dated_one_is_kept(self):
        # (here) one MARR says that Leopold and Agnes married, citing the
        # tree, and another dates the wedding, citing the judgment: each
        # gives the wedding, citing its own source
        text = cited(TREE, JUDGMENT) + family(
            "1 MARR Y", "2 SOUR @S1@", "1 MARR", "2 DATE 8 FEB 1947",
            "2 SOUR @S2@",
        )  # fmt: skip
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {}
        assert sources(text, "f5", "started_by") == [
            (None, f"{PARTNERS} | 1 MARR Y | 2 SOUR @S1@ | cited: {TREE}"),
            (
                None,
                f"{PARTNERS} | 1 MARR | 2 DATE 8 FEB 1947 | 2 SOUR @S2@"
                f" | cited: {JUDGMENT}",
            ),
        ]

    @pytest.mark.parametrize(
        ("divorce", "warnings"),
        [
            (["1 DIV Y"], []),
            (
                ["1 DIV", "2 DATE NEVER EVER"],
                [
                    "line 14: unparsed date 'NEVER EVER' — no point_in_time "
                    "claim emitted"
                ],
            ),
        ],
        ids=["undated", "unparsed"],
    )
    def test_an_undated_divorces_citation_is_folded_into_its_end(
        self, divorce, warnings
    ):
        # (here) the family record says that Leopold and Agnes divorced,
        # citing the judgment, and not when
        text = cited(JUDGMENT) + family(*WEDDING, *divorce, "2 SOUR @S1@")
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {}
        assert result.warnings == warnings
        lines = " | ".join([*divorce, "2 SOUR @S1@"])
        assert sources(text, "f5", "ended_by") == [
            (None, f"{PARTNERS} | {lines} | cited: {JUDGMENT}")
        ]

    def test_a_divs_citation_is_counted_when_it_states_no_divorce(self):
        text = cited(JUDGMENT) + family(*WEDDING, "1 DIV", "2 SOUR @S1@")
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {"DIV": 1, "SOUR": 1}
        assert sources(text, "f5", "ended_by") == []

    @pytest.mark.parametrize(
        ("partners", "counted"),
        [([], {"FAM.SOUR": 1, "SOUR": 1}), (["1 HUSB @I6@"], {})],
        ids=["no-claim", "a-parent"],
    )
    def test_a_familys_citation_is_counted_when_it_gives_no_claim(
        self, partners, counted
    ):
        # (here) Felix and Ida's family record, citing the compensation
        # file, names Rudolf as their son, and Felix as his father or
        # neither parent: with a parent, the parent claim holds the
        # citation, and with none, no claim does
        text = cited(COMPENSATION) + "\n".join(
            [
                "0 @I6@ INDI",
                "1 NAME Felix /Löbig/",
                "0 @I12@ INDI",
                "1 NAME Rudolf /Löbig/",
                "0 @F3@ FAM",
                *partners,
                "1 CHIL @I12@",
                "1 SOUR @S1@",
                "0 TRLR",
                "",
            ]
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == counted
        fathers = claims_for(result.batch, "i12", "father")
        assert [
            father.source.transcription_excerpt.text for father in fathers
        ] == [
            f"0 @F3@ FAM | {partner} | 1 CHIL @I12@ | 1 SOUR @S1@"
            f" | cited: {COMPENSATION}"
            for partner in partners
        ]

    @pytest.mark.parametrize("tag", ["HUSB", "WIFE"])
    def test_a_further_partner_is_left_out_of_a_unions_excerpts(self, tag):
        # (here) Leopold and Agnes's family record names a further HUSB or
        # WIFE, which the census counts: the union's excerpts name the
        # partners its claims name, and no other
        text = (
            "0 @I99@ INDI\n1 NAME A /B/\n"
            + cited(JUDGMENT)
            + family(f"1 {tag} @I99@", *WEDDING, "2 SOUR @S1@")
        )
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == {
            f"FAM.{tag}": 1
        }
        assert sources(text, "f5", "participant") == [(None, PARTNERS)] * 2
        assert sources(text, "f5", "started_by") == [
            (
                None,
                f"{PARTNERS} | 1 MARR | 2 DATE 8 FEB 1947 | 2 PLAC {FLOERBURG}"
                f" | 2 SOUR @S1@ | cited: {JUDGMENT}",
            )
        ]

    def test_a_marriage_citation_is_no_citation_of_the_parents(self):
        # (here) Felix and Ida's family record gives Leopold as their son,
        # and cites the compensation file's fourth sheet beneath its MARR:
        # the sheet attests the marriage, not Leopold's parents, whose
        # claims keep the whole family record as their excerpt
        text = cited(COMPENSATION) + ida_and_felix(
            "1 MARR Y", "2 SOUR @S1@", "3 PAGE Bl. 4"
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        for prop in ("father", "mother"):
            (parent,) = claims_for(batch, "i11", prop)
            assert parent.source.page_number is None
            assert parent.source.transcription_excerpt.text == (
                "0 @F3@ FAM | 1 HUSB @I6@ | 1 WIFE @I7@ | 1 CHIL @I11@"
                " | 1 MARR Y | 2 SOUR @S1@ | 3 PAGE Bl. 4"
            )
        (started,) = claims_for(batch, "f3", "started_by")
        assert started.source.page_number == "Bl. 4"

    def test_the_fixtures_union_claims_hold_the_lines_that_give_them(self):
        # Sebastian and Hildegard's family record: their union's claims
        # hold no CHIL line, and their children's parent claims hold every
        # line
        batch = fixture_result().batch
        partners = "0 @F2@ FAM | 1 HUSB @I3@ | 1 WIFE @I4@"
        wedding = (
            f"{partners} | 1 MARR | 2 TYPE civil | 2 DATE 15 DEC 1905"
            f" | 2 PLAC {FLOERBURG}"
        )
        for claim in claims_for(batch, "f2", "participant"):
            assert claim.source.transcription_excerpt.text == partners
        for claim in [
            *claims_for(batch, "f2", "started_by"),
            *claims_for(batch, "f2-civil-wedding"),
        ]:
            assert claim.source.transcription_excerpt.text == wedding
        (father,) = claims_for(batch, "i8", "father")
        assert father.source.transcription_excerpt.text == (
            "0 @F2@ FAM | 1 HUSB @I3@ | 1 WIFE @I4@ | 1 CHIL @I10@"
            " | 1 CHIL @I9@ | 1 CHIL @I8@ | 1 MARR | 2 TYPE civil"
            f" | 2 DATE 15 DEC 1905 | 2 PLAC {FLOERBURG}"
        )


class TestCitationsAndMediaNotHeld:
    """A SOUR that no claim's reference holds, and an OBJE that no media
    item stands for, is counted by the census where it stands, named with
    its parent's tag, as is a level-0 SOUR or OBJE record that nothing the
    adapter maps cites (ADR-0021; #324)."""

    @pytest.mark.parametrize(
        ("lines", "counted"),
        [
            # the tree cites Eugen himself, rather than a fact of his
            (["1 SOUR @S1@"], {"INDI.SOUR": 1}),
            # (here) it cites his birth's year, or its place, rather than
            # his birth: the birth's claims cite neither
            (["1 BIRT", "2 DATE 1836", "3 SOUR @S1@"], {"DATE.SOUR": 1}),
            (
                [
                    "1 BIRT",
                    "2 PLAC Bad Flörburg, Hesse, Germany",
                    "3 SOUR @S1@",
                ],
                {"PLAC.SOUR": 1},
            ),
            # a birth that gives no claim, and a NAME that no claim types,
            # with no TYPE or with one the adapter maps to no property
            (["1 BIRT Y", "2 SOUR @S1@"], {"BIRT.SOUR": 1}),
            (["2 SOUR @S1@"], {"NAME.SOUR": 1}),
            (["2 TYPE other", "2 SOUR @S1@"], {"NAME.SOUR": 1}),
            # his link to his parents' family, and to his own
            (["1 FAMC @F1@", "2 SOUR @S1@"], {"FAMC.SOUR": 1}),
            (["1 FAMS @F1@", "2 SOUR @S1@"], {"FAMS.SOUR": 1}),
            # a sex the adapter maps to none
            (["1 SEX U", "2 SOUR @S1@"], {"SEX.SOUR": 1}),
        ],
        ids=[
            "INDI",
            "DATE",
            "PLAC",
            "BIRT-Y",
            "NAME",
            "NAME-other-TYPE",
            "FAMC",
            "FAMS",
            "SEX",
        ],
    )
    def test_a_citation_that_no_claim_holds_is_counted(self, lines, counted):
        result = adapt_gedcom(cited(TREE) + eugen(*lines), artifact=EXPORT)
        # and the tree's record, which no folded citation cites
        assert result.unsupported_tags == {**counted, "SOUR": 1}
        for claim in result.batch.claims:
            assert claim.source.page_number is None
            assert " | cited: " not in claim.source.transcription_excerpt.text

    @pytest.mark.parametrize("link", ["1 CHIL @I11@", "1 HUSB @I6@"])
    def test_a_citation_of_a_familys_link_to_a_person_is_counted(self, link):
        # (here) Felix and Ida's family record cites the compensation file
        # beneath its line that names Leopold, or Felix: the parent claims
        # and the union's cite the family, and none folds it
        text = cited(COMPENSATION) + ida_and_felix().replace(
            f"{link}\n", f"{link}\n2 SOUR @S1@\n"
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        tag = link.split()[1]
        assert result.unsupported_tags == {f"{tag}.SOUR": 1, "SOUR": 1}
        assert stated(text, "i11", "father") == [("Felix Löbig", [])]
        for claim in result.batch.claims:
            assert claim.source.page_number is None
            assert " | cited: " not in claim.source.transcription_excerpt.text

    @pytest.mark.parametrize(
        ("cites", "counted"),
        [(["1 BIRT", "2 DATE 1836", "2 SOUR @S1@"], {}), ([], {"SOUR": 1})],
        ids=["cited", "uncited"],
    )
    def test_a_source_record_no_folded_citation_cites_is_counted(
        self, cites, counted
    ):
        # the burial register's record, which (here) the export's birth
        # cites, or which nothing cites
        text = cited(BURIAL_REGISTER) + eugen(*cites)
        assert adapt_gedcom(text, artifact=EXPORT).unsupported_tags == counted

    @pytest.mark.parametrize(
        ("lines", "counted"),
        [
            # (here) the export gives the studio portrait beneath Eugen's
            # death, which it says nothing more of, or embeds the gravestone
            # photograph beneath his burial
            (["1 DEAT Y", "2 OBJE @O1@"], {"DEAT.OBJE": 1}),
            (
                [
                    "1 BURI",
                    "2 PLAC Friedhof Flörburg, Hesse, Germany",
                    "2 OBJE",
                    "3 FILE gravestone-1956.jpg",
                ],
                {"BURI.OBJE": 1},
            ),
        ],
        ids=["DEAT-link", "BURI-inline"],
    )
    def test_an_object_beneath_an_event_is_counted(self, lines, counted):
        result = adapt_gedcom(PORTRAIT + eugen(*lines), artifact=EXPORT)
        # and the portrait's record, which no image claim cites
        assert result.unsupported_tags == {**counted, "OBJE": 1}
        assert not [e for e in result.batch.entities if e.kind == "artifact"]
        assert not claims_for(result.batch, "i1", "image")

    @pytest.mark.parametrize(
        ("imaged", "counted"),
        [([], {"FAM.OBJE": 1, "OBJE": 1}), (["1 OBJE @O1@"], {"FAM.OBJE": 1})],
        ids=["family-alone", "and-Eugen"],
    )
    def test_an_object_of_a_family_is_counted(self, imaged, counted):
        # (here) the export gives the studio portrait beneath Eugen and
        # Cäcilie's family record, and beneath Eugen's own, or not
        text = PORTRAIT + "\n".join(
            [
                "0 @I1@ INDI",
                "1 NAME Eugen /Löbig/",
                *imaged,
                "0 @I2@ INDI",
                "1 NAME Cäcilie /Tarle/",
                "0 @F1@ FAM",
                "1 HUSB @I1@",
                "1 WIFE @I2@",
                "1 OBJE @O1@",
                "0 TRLR",
                "",
            ]
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == counted
        assert len(claims_for(result.batch, "i1", "image")) == len(imaged)
        assert not claims_for(result.batch, "i2", "image")

    def test_a_person_skipped_for_want_of_a_name_has_their_citations_counted(
        self,
    ):
        # (here) the export gives the infant of 1922 no name, and cites the
        # tree for the birth and links the studio portrait: the person is
        # skipped, with the warning, and no claim folds the citation or
        # maps the portrait, so the census counts both, as it does the
        # records they point to; the birth itself is taken as read
        text = (
            cited(TREE)
            + PORTRAIT
            + "\n".join(
                [
                    "0 @I13@ INDI",
                    "1 SEX U",
                    "1 BIRT",
                    "2 DATE 1922",
                    "2 SOUR @S1@",
                    "1 OBJE @O1@",
                    "0 TRLR",
                    "",
                ]
            )
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.warnings == [
            "@I13@: no NAME gives a name; skipped entirely, with the family "
            "links that name it"
        ]
        assert result.unsupported_tags == {
            "BIRT.SOUR": 1,
            "OBJE": 2,
            "SOUR": 1,
        }

    @pytest.mark.parametrize(
        ("objects", "counted"),
        [
            # a scan embedded in the register's record
            (["1 OBJE", "2 FILE burial-register-1909.jpg"], {"SOUR.OBJE": 1}),
            # a link to a scan of the sheet, whose record no image claim
            # cites either
            (["1 OBJE @O2@"], {"SOUR.OBJE": 1, "OBJE": 1}),
        ],
        ids=["embedded", "linked"],
    )
    def test_an_object_within_a_cited_source_record_is_counted(
        self, objects, counted
    ):
        # (here) the burial register's record holds a scan of it, and the
        # export cites the register for Eugen's burial: the citation is
        # folded, and the scan, which no media item stands for, counted
        text = (
            "\n".join(["0 @S1@ SOUR", f"1 TITL {BURIAL_REGISTER}", *objects])
            + "\n"
            + (SHEET_SCAN if "1 OBJE @O2@" in objects else "")
            + eugen(
                "1 BURI",
                "2 PLAC Friedhof Flörburg, Hesse, Germany",
                "2 SOUR @S1@",
            )
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == counted
        (burial,) = claims_for(result.batch, "i1", "burial_place")
        assert burial.source.transcription_excerpt.text.endswith(
            f" | cited: {BURIAL_REGISTER}"
        )

    @pytest.mark.parametrize(
        ("text", "counted"),
        [
            # the export's citation of the burial register for Eugen's
            # burial holds a scan of the sheet, embedded or linked
            (
                cited(BURIAL_REGISTER)
                + eugen(
                    "1 BURI",
                    "2 PLAC Friedhof Flörburg, Hesse, Germany",
                    "2 SOUR @S1@",
                    "3 PAGE Bl. 12",
                    "3 OBJE",
                    "4 FILE burial-register-1909.jpg",
                ),
                {"SOUR.OBJE": 1},
            ),
            (
                cited(BURIAL_REGISTER)
                + SHEET_SCAN
                + eugen(
                    "1 BURI",
                    "2 PLAC Friedhof Flörburg, Hesse, Germany",
                    "2 SOUR @S1@",
                    "3 PAGE Bl. 12",
                    "3 OBJE @O2@",
                ),
                {"SOUR.OBJE": 1, "OBJE": 1},
            ),
            # the studio portrait's record cites the tree, and its citation
            # holds a scan of the tree's sheet
            (
                cited(TREE)
                + PORTRAIT
                + "1 SOUR @S1@\n2 OBJE\n3 FILE tree-sheet-1.jpg\n"
                + eugen("1 OBJE @O1@"),
                {"SOUR.OBJE": 1},
            ),
            # Eugen's link to the portrait cites the tree likewise
            (
                cited(TREE)
                + PORTRAIT
                + eugen(
                    "1 OBJE @O1@",
                    "2 SOUR @S1@",
                    "3 OBJE",
                    "4 FILE tree-sheet-1.jpg",
                ),
                {"SOUR.OBJE": 1},
            ),
        ],
        ids=["embedded", "linked", "in-an-object", "in-a-link"],
    )
    def test_an_object_within_a_folded_citation_is_counted(
        self, text, counted
    ):
        # the citation is folded, lines and all, and the scan, which no
        # media item stands for, counted, wherever the folded citation
        # stands
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == counted
        assert result.batch.claims

    def test_a_further_files_citation_is_counted_with_it(self):
        # (here) the export cites the tree beneath the studio portrait's
        # back: the back's FILE is counted, and stands for its citation,
        # which is not counted again; the tree's record, which no folded
        # citation cites, is
        text = (
            cited(TREE)
            + PORTRAIT
            + "1 FILE studio-portrait-1890s-back.jpg\n2 SOUR @S1@\n"
            + eugen("1 OBJE @O1@")
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {"OBJE.FILE": 1, "SOUR": 1}

    @pytest.mark.parametrize(
        ("portrait", "link", "counted"),
        [
            # a citation beneath the portrait's FILE
            (["1 FILE studio-portrait-1890s.jpg", "2 SOUR @S2@"], [], "FILE"),
            # a citation beneath a NOTE of the person's link
            (
                ["1 FILE studio-portrait-1890s.jpg"],
                ["2 NOTE Eugen stands at the back", "3 SOUR @S2@"],
                "NOTE",
            ),
        ],
        ids=["FILE", "link-NOTE"],
    )
    def test_a_citation_within_a_mapped_object_is_counted(
        self, portrait, link, counted
    ):
        # (here) the studio portrait's record cites the tree, which names
        # the sitters, and the postcard stands beneath its FILE or beneath
        # a NOTE of Eugen's link to it: the tree's citation is folded, and
        # the postcard's, which no claim holds, counted, as is the
        # postcard's record, which no folded citation cites
        text = (
            cited(TREE, POSTCARD)
            + "\n".join(["0 @O1@ OBJE", *portrait, "1 SOUR @S1@"])
            + "\n"
            + eugen("1 OBJE @O1@", *link)
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {f"{counted}.SOUR": 1, "SOUR": 1}
        (image,) = claims_for(result.batch, "i1", "image")
        assert image.source.transcription_excerpt.text.endswith(
            f" | cited: {TREE}"
        )

    def test_a_citation_of_a_persons_link_to_an_object_is_folded(self):
        # (here) the export cites the tree's first sheet beneath Eugen's
        # link to the studio portrait: the link's lines come first in the
        # image claim's excerpt
        text = (
            cited(TREE)
            + PORTRAIT
            + eugen("1 OBJE @O1@", "2 SOUR @S1@", "3 PAGE sheet 1")
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {}
        (image,) = claims_for(result.batch, "i1", "image")
        assert image.source.page_number == "sheet 1"
        assert image.source.transcription_excerpt.text == (
            "1 OBJE @O1@ | 2 SOUR @S1@ | 3 PAGE sheet 1 | "
            f"{PORTRAIT_LINES} | cited: {TREE}"
        )

    def test_a_bare_link_gives_the_objects_lines_alone(self):
        batch = adapt_gedcom(
            PORTRAIT + eugen("1 OBJE @O1@"), artifact=EXPORT
        ).batch
        (image,) = claims_for(batch, "i1", "image")
        assert image.source.transcription_excerpt.text == PORTRAIT_LINES

    def test_a_citation_of_an_objects_record_is_folded(self):
        # (here) the studio portrait's record cites the tree's first sheet,
        # which names the sitters: each image claim cites it
        text = (
            cited(TREE)
            + PORTRAIT
            + "1 SOUR @S1@\n2 PAGE sheet 1\n"
            + eugen("1 OBJE @O1@")
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert result.unsupported_tags == {}
        (image,) = claims_for(result.batch, "i1", "image")
        assert image.source.page_number == "sheet 1"
        assert image.source.transcription_excerpt.text == (
            f"{PORTRAIT_LINES} | 1 SOUR @S1@ | 2 PAGE sheet 1 | cited: {TREE}"
        )


class TestEveryPageAndTitle:
    """Every PAGE of a citation, every TITL of a source record and every
    TITL of an object is read, and every FILE of an object is mapped or
    counted: an object's media item stands for its first file (#324)."""

    def test_every_page_of_a_citation_is_a_locator(self):
        # (here) the export cites the two sheets of the burial register
        # that give Eugen's burial
        text = cited(BURIAL_REGISTER) + eugen(
            "1 BURI",
            "2 DATE 20 MAR 1909",
            "2 PLAC Friedhof Flörburg, Hesse, Germany",
            "2 SOUR @S1@",
            "3 PAGE Bl. 12",
            "3 PAGE Bl. 13",
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (burial,) = claims_for(batch, "i1", "burial_place")
        assert burial.source.page_number == "Bl. 12 ; Bl. 13"

    @pytest.mark.parametrize(
        ("pages", "expected"),
        [
            # two citations, each at a long place in the register: the
            # page number keeps the first, and marks the other left out
            (
                [
                    [f"3 PAGE {text_of(250, ' Bl. 12')}"],
                    [f"3 PAGE {text_of(250, ' Bl. 13')}"],
                ],
                f"{text_of(250, ' Bl. 12')} ; …",
            ),
            # one place longer than a page number holds: cut within it
            ([[f"3 PAGE {text_of(450)}"]], f"{text_of(450)[:396]} ; …"),
            # two that fill the page number to its last character
            (
                [
                    [f"3 PAGE {text_of(200, ' Bl. 12')}"],
                    [f"3 PAGE {text_of(197, ' Bl. 13')}"],
                ],
                f"{text_of(200, ' Bl. 12')} ; {text_of(197, ' Bl. 13')}",
            ),
        ],
        ids=["two-pages", "one-page", "exactly-400"],
    )
    def test_a_page_number_holds_at_most_400_characters(self, pages, expected):
        # (here) the export cites the burial register for Eugen's burial at
        # length, as a Wikibase string holds at most 400 characters
        citations = [
            line
            for number, page in enumerate(pages, start=1)
            for line in (f"2 SOUR @S{number}@", *page)
        ]
        text = cited(BURIAL_REGISTER, BURIAL_REGISTER) + eugen(
            "1 BURI", "2 PLAC Friedhof Flörburg, Hesse, Germany", *citations
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (burial,) = claims_for(batch, "i1", "burial_place")
        assert burial.source.page_number == expected
        assert len(expected) <= 400

    @pytest.mark.parametrize(
        ("pages", "expected"),
        [
            (["3 PAGE", "3 PAGE Bl. 12"], "Bl. 12"),
            (["3 PAGE"], None),
            (
                ["3 PAGE Bl. 12, " + unicodedata.normalize("NFD", "Löbig")],
                "Bl. 12, Löbig",
            ),
        ],
        ids=["empty-and-page", "empty", "NFD"],
    )
    def test_a_page_number_is_each_page_given_in_nfc(self, pages, expected):
        # (here) the burial register's citation gives an empty PAGE, or a
        # page spelt in decomposed form: an empty one is no locator, and a
        # page number is in NFC, as the instance stores it
        text = cited(BURIAL_REGISTER) + eugen(
            "1 BURI",
            "2 PLAC Friedhof Flörburg, Hesse, Germany",
            "2 SOUR @S1@",
            *pages,
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (burial,) = claims_for(batch, "i1", "burial_place")
        assert burial.source.page_number == expected

    def test_every_title_of_a_source_record_names_it(self):
        # the Flörburg address book of 1912, (here) titled in German and in
        # English, and in German again
        text = (
            "0 @S1@ SOUR\n1 TITL Adressbuch der Stadt Flörburg 1912\n"
            "1 TITL Flörburg address book 1912\n"
            "1 TITL Adressbuch der Stadt Flörburg 1912\n"
        ) + person(
            "I16",
            "Ursula /Löbig/",
            "1 RESI",
            "2 DATE 1912",
            "2 PLAC Angerring 57, Flörburg, Hesse, Germany",
            "2 SOUR @S1@",
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (residence,) = claims_for(batch, "i16", "residence")
        assert residence.source.transcription_excerpt.text.endswith(
            " | cited: Adressbuch der Stadt Flörburg 1912"
            " / Flörburg address book 1912"
        )

    @pytest.mark.parametrize(
        ("titles", "label", "aliases"),
        [
            # beneath the FILE, where real exports nest it, and beneath the
            # object, as GEDCOM 5.5.1 has an object embedded in a record
            (
                [f"2 TITL {GRAVESTONE}", "1 TITL Grabstein der Familie Löbig"],
                GRAVESTONE,
                ["Grabstein der Familie Löbig"],
            ),
            # the object's own, when the FILE has none
            (
                ["1 TITL Grabstein der Familie Löbig"],
                "Grabstein der Familie Löbig",
                [],
            ),
            # two beneath the FILE, the one restating the other
            ([f"2 TITL {GRAVESTONE}", f"2 TITL {GRAVESTONE}"], GRAVESTONE, []),
        ],
        ids=["FILE-and-object", "object", "restated"],
    )
    def test_every_title_of_an_object_labels_its_item_or_is_an_alias(
        self, titles, label, aliases
    ):
        # the gravestone photograph, (here) linked from Eugen and titled as
        # given
        text = (
            "\n".join(["0 @O1@ OBJE", "1 FILE gravestone-1956.jpg", *titles])
            + "\n"
            + eugen("1 OBJE @O1@")
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        (media,) = [e for e in result.batch.entities if e.kind == "artifact"]
        assert (media.label, media.aliases) == (label, aliases)
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        ("objects", "link"),
        [
            (
                "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
                f"2 TITL {STUDIO}\n1 FILE studio-portrait-1890s-back.jpg\n"
                f"2 TITL {STUDIO_BACK}\n",
                ["1 OBJE @O1@"],
            ),
            (
                "",
                [
                    "1 OBJE",
                    "2 FILE studio-portrait-1890s.jpg",
                    f"3 TITL {STUDIO}",
                    "2 FILE studio-portrait-1890s-back.jpg",
                    f"3 TITL {STUDIO_BACK}",
                ],
            ),
        ],
        ids=["record", "embedded"],
    )
    def test_an_objects_further_file_is_counted(self, objects, link):
        # the studio portrait and its back, where the sitters' names are
        # pencilled: (here) one object gives both files, in a record or
        # embedded beneath Eugen. Its item stands for the front, and the
        # back is counted, its title with it
        result = adapt_gedcom(objects + eugen(*link), artifact=EXPORT)
        assert result.unsupported_tags == {"OBJE.FILE": 1}
        (media,) = [e for e in result.batch.entities if e.kind == "artifact"]
        assert (media.label, media.aliases) == (STUDIO, [])
        assert len(claims_for(result.batch, "i1", "image")) == 1

    def test_a_further_files_title_labels_nothing(self):
        # (here) only the back's file is titled: the item takes the front's
        # file name
        text = (
            "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
            "1 FILE studio-portrait-1890s-back.jpg\n"
            f"2 TITL {STUDIO_BACK}\n"
        ) + eugen("1 OBJE @O1@")
        result = adapt_gedcom(text, artifact=EXPORT)
        (media,) = [e for e in result.batch.entities if e.kind == "artifact"]
        assert (media.label, media.aliases) == (
            "studio-portrait-1890s.jpg",
            [],
        )
        assert result.unsupported_tags == {"OBJE.FILE": 1}


def note_of(length: int, ending: str = "") -> str:
    """A NOTE line of at least `length` characters on Gustav's birth
    register extract, ending (here) with `ending`."""
    sentence = (
        "The extract was issued by the Standesamt Flörburg in 1938 for "
        "Gustav's emigration."
    )
    return "2 NOTE " + " ".join(
        [sentence] * (length // len(sentence) + 1)
        + ([ending] if ending else [])
    )


def gustavs_births(*births: list[str]) -> str:
    """The birth register extract's and the tree's records, and Gustav's
    record, with a BIRT of each of the given lists of lines beneath it."""
    return cited(BIRTH_EXTRACT, TREE) + person(
        "I8", "Gustav /Löbig/", *(line for birth in births for line in birth)
    )


def clipped(whole: str) -> str:
    """The mark a clipped excerpt ends with, of the excerpt `whole`."""
    return f" | clipped, sha1 {hashlib.sha1(whole.encode()).hexdigest()[:12]}"


class TestExcerptClip:
    """An excerpt holds at most 400 characters, as a Wikibase text value
    does. A longer one keeps every line of the citations folded into its
    claim, and their titles, and as many of its other lines, from the
    first, as fit, and it ends with a key of its whole text, so that
    clipping never makes one claim of two that the whole excerpts keep
    apart, such as two citations of one fact (#350)."""

    def test_two_citations_of_one_fact_beneath_a_long_note_stay_apart(self):
        # Gustav was born on 7 January 1919, per his birth register
        # extract and per the tree: (here) the export gives the birth
        # twice, beneath one long NOTE, citing each with no PAGE, so only
        # the citation tells the two apart
        note = note_of(450)
        head = ["1 BIRT", "2 DATE 7 JAN 1919", f"2 PLAC {FLOERBURG}", note]
        text = gustavs_births([*head, "2 SOUR @S1@"], [*head, "2 SOUR @S2@"])
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        births = claims_for(batch, "i8", "date_of_birth")
        start = " | ".join(head)
        assert [c.source.transcription_excerpt.text for c in births] == [
            f"1 BIRT | 2 DATE 7 JAN 1919 | 2 PLAC {FLOERBURG} | …"
            f" | 2 SOUR {pointer} | cited: {title}"
            + clipped(f"{start} | 2 SOUR {pointer} | cited: {title}")
            for pointer, title in (("@S1@", BIRTH_EXTRACT), ("@S2@", TREE))
        ]

    def test_each_run_of_lines_left_out_is_marked(self):
        # (here) a long NOTE stands before the citation, and another after
        # it: the citation's lines stay, between the two marks
        before, after = note_of(300), note_of(300)
        lines = ["1 BIRT", "2 DATE 7 JAN 1919", before, "2 SOUR @S1@"]
        lines += ["3 PAGE Nr. 12", after]
        batch = adapt_gedcom(gustavs_births(lines), artifact=EXPORT).batch
        (birth,) = claims_for(batch, "i8", "date_of_birth")
        whole = " | ".join(lines) + f" | cited: {BIRTH_EXTRACT}"
        assert birth.source.transcription_excerpt.text == (
            "1 BIRT | 2 DATE 7 JAN 1919 | … | 2 SOUR @S1@ | 3 PAGE Nr. 12"
            f" | … | cited: {BIRTH_EXTRACT}" + clipped(whole)
        )

    def test_a_birth_that_differs_only_past_the_clip_is_a_claim_of_its_own(
        self,
    ):
        # (here) the export gives the birth twice, each citing the extract,
        # beneath NOTEs that differ only at their ends: two places of the
        # export, each with a reference of its own, as unclipped ones are
        text = gustavs_births(
            ["1 BIRT", "2 DATE 7 JAN 1919", note_of(450, "Seen by Klara.")],
            ["1 BIRT", "2 DATE 7 JAN 1919", note_of(450, "Seen by Gustav.")],
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        excerpts = [
            c.source.transcription_excerpt.text
            for c in claims_for(batch, "i8", "date_of_birth")
        ]
        assert len(excerpts) == 2 and excerpts[0] != excerpts[1]
        assert all(len(excerpt) <= 400 for excerpt in excerpts)

    def test_a_first_line_too_long_to_keep_is_cut_beside_its_citation(self):
        # (here) the export writes Gustav's trade over and over, citing the
        # naturalisation: the citation is kept whole, and the occupation's
        # own line, which names the record, cut to the room it leaves
        trade = "1 OCCU " + "Master hatter, " * 30
        lines = [trade.strip(), "2 SOUR @S1@"]
        title = "Naturalisation of Gustave Lobig"
        text = cited(title) + person("I8", "Gustav /Löbig/", *lines)
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (occupation,) = claims_for(batch, "i8", "occupation")
        whole = " | ".join(lines) + f" | cited: {title}"
        rest = f" | 2 SOUR @S1@ | cited: {title}" + clipped(whole)
        assert occupation.source.transcription_excerpt.text == (
            trade[: 400 - len(rest) - 1].rstrip() + "…" + rest
        )

    @pytest.mark.parametrize(
        ("after", "mark"),
        [([], ""), (["2 DATE 1909"], " | …")],
        ids=["alone", "before-a-date"],
    )
    def test_a_first_line_too_long_to_keep_is_cut_to_the_room_left(
        self, after, mark
    ):
        # (here) the export writes Eugen's trade over and over and cites
        # nothing: the occupation's own line is cut to the room left, and
        # a DATE after it, which no longer fits, is left out
        trade = "1 OCCU " + "Master hatter, " * 30
        lines = [trade.strip(), *after]
        batch = adapt_gedcom(eugen(*lines), artifact=EXPORT).batch
        (occupation,) = claims_for(batch, "i1", "occupation")
        tail = mark + clipped(" | ".join(lines))
        assert occupation.source.transcription_excerpt.text == (
            trade[: 400 - len(tail) - 1].rstrip() + "…" + tail
        )

    @pytest.mark.parametrize(
        ("room", "first"),
        [(9, "1 OCCU M…"), (8, "…")],
        ids=["tag-and-a-letter", "tag-alone"],
    )
    def test_a_cut_first_line_keeps_its_tag_and_some_value(self, room, first):
        # (here) the export writes Eugen's trade over and over, and cites
        # the burial register for it at such length that the citation
        # leaves the occupation's line `room` characters: a cut keeps the
        # line's level and tag and a letter of its value, or the line is
        # left out
        trade = ("1 OCCU " + "Master hatter, " * 30).strip()
        head = " | 2 SOUR @S1@ | 3 PAGE "
        cited_part = f" | cited: {BURIAL_REGISTER}"
        page = text_of(400 - 29 - room - len(head) - len(cited_part))
        lines = [trade, "2 SOUR @S1@", f"3 PAGE {page}"]
        text = cited(BURIAL_REGISTER) + eugen(*lines)
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (occupation,) = claims_for(batch, "i1", "occupation")
        whole = " | ".join(lines) + cited_part
        assert occupation.source.transcription_excerpt.text == (
            first + head + page + cited_part + clipped(whole)
        )

    def test_citations_that_fill_the_room_exactly_are_kept_whole(self):
        # (here) the burial register's citation of Eugen as a master hatter
        # is exactly as long as the room a clip leaves: the excerpt keeps
        # it whole, at 400 characters, and no other line
        head = " | 2 SOUR @S1@ | 3 PAGE "
        cited_part = f" | cited: {BURIAL_REGISTER}"
        page = text_of(400 - 29 - len("…") - len(head) - len(cited_part))
        lines = ["1 OCCU Master hatter", "2 DATE 1909", "2 SOUR @S1@"]
        lines.append(f"3 PAGE {page}")
        text = cited(BURIAL_REGISTER) + eugen(*lines)
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (occupation,) = claims_for(batch, "i1", "occupation")
        whole = " | ".join(lines) + cited_part
        excerpt = occupation.source.transcription_excerpt.text
        assert excerpt == "…" + head + page + cited_part + clipped(whole)
        assert len(excerpt) == 400

    def test_a_continuation_of_a_citing_line_waits_its_turn(self):
        # (here) the DIV line the judgment's citation stands beneath runs
        # on over a CONT line, after a NOTE on Leopold's line in the family
        # record: the clip keeps the DIV line before any other, but its
        # continuation only in its turn, after the NOTE, where it no longer
        # fits
        note = f"2 NOTE {text_of(170)}"
        divorce = "1 DIV Divorced by the Landgericht Rekau"
        more = "by its judgment of 2 January 1957"
        after = ["2 DATE 2 JAN 1957", "2 SOUR @S1@", f"2 NOTE {text_of(300)}"]
        head = ["1 HUSB @I11@", note, "1 WIFE @I17@"]
        text = cited(JUDGMENT) + leopold_and_agnes(
            *head, divorce, f"2 CONT {more}", *after
        )
        # the CONT line is a line of the excerpt of its own
        pieces = ["0 @F5@ FAM", *head, divorce, more, *after]
        whole = " | ".join(pieces) + f" | cited: {JUDGMENT}"
        ((_, excerpt),) = sources(text, "f5", "ended_by")
        assert excerpt == (
            f"0 @F5@ FAM | 1 HUSB @I11@ | {note} | 1 WIFE @I17@"
            f" | {divorce} | … | 2 SOUR @S1@ | … | cited: {JUDGMENT}"
            + clipped(whole)
        )

    def test_a_family_of_many_children_keeps_its_citation(self):
        # (here) Sebastian and Hildegard's family record lists forty
        # children the export gives no record of, then Gustav, and cites
        # the tree's second sheet: each of Gustav's parent claims keeps the
        # citation, after as many of the family's lines as fit
        others = [f"1 CHIL @I{number}@" for number in range(100, 140)]
        family_lines = [
            "0 @F2@ FAM",
            "1 HUSB @I3@",
            "1 WIFE @I4@",
            *others,
            "1 CHIL @I8@",
            "1 SOUR @S1@",
            "2 PAGE sheet 2",
        ]
        text = cited(TREE) + "\n".join(
            [
                "0 @I3@ INDI",
                "1 NAME Sebastian /Löbig/",
                "0 @I4@ INDI",
                "1 NAME Hildegard /Laudler/",
                "0 @I8@ INDI",
                "1 NAME Gustav /Löbig/",
                *family_lines,
                "0 TRLR",
                "",
            ]
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        for prop in ("father", "mother"):
            (parent,) = claims_for(batch, "i8", prop)
            excerpt = parent.source.transcription_excerpt.text
            assert parent.source.page_number == "sheet 2"
            assert len(excerpt) <= 400
            assert excerpt.startswith("0 @F2@ FAM | 1 HUSB @I3@ | ")
            assert excerpt.endswith(
                f" | … | 1 SOUR @S1@ | 2 PAGE sheet 2 | cited: {TREE}"
                + clipped(" | ".join(family_lines) + f" | cited: {TREE}")
            )

    def test_a_long_birth_restated_line_for_line_is_one_claim(self):
        birth = ["1 BIRT", "2 DATE 7 JAN 1919", note_of(450), "2 SOUR @S1@"]
        batch = adapt_gedcom(
            gustavs_births(birth, birth), artifact=EXPORT
        ).batch
        assert len(claims_for(batch, "i8", "date_of_birth")) == 1

    def test_citations_too_long_to_keep_whole_leave_their_key(self):
        # (here) each citation quotes the extract at length: the excerpt
        # keeps what fits, from the first line, and the two stay apart
        quote = "3 DATA\n4 TEXT " + note_of(400)[len("2 NOTE ") :]
        births = [
            ["1 BIRT", "2 DATE 7 JAN 1919", f"2 SOUR {pointer}", quote]
            for pointer in ("@S1@", "@S2@")
        ]
        batch = adapt_gedcom(gustavs_births(*births), artifact=EXPORT).batch
        excerpts = [
            c.source.transcription_excerpt.text
            for c in claims_for(batch, "i8", "date_of_birth")
        ]
        assert [
            excerpt[: excerpt.index(" | 3 DATA")] for excerpt in excerpts
        ] == [
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 SOUR @S1@",
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 SOUR @S2@",
        ]
        for excerpt, birth, title in zip(
            excerpts, births, (BIRTH_EXTRACT, TREE), strict=True
        ):
            lines = " | ".join(birth).replace("\n", " | ")
            whole = f"{lines} | cited: {title}"
            # cut back to the last whole line that fits: the quote's TEXT
            # line is left out whole
            assert excerpt == (
                " | ".join(birth[:3]) + " | 3 DATA | …" + clipped(whole)
            )

    @pytest.mark.parametrize(
        "lines",
        [
            # an occupation named at length
            ["1 OCCU " + "Master hatter " * 40],
            # a NOTE of a placeholder sign (U+2ADC) that NFC writes as two
            # characters: longer as the instance stores it than as written
            ["1 BIRT", "2 DATE 7 JAN 1919", "2 NOTE " + "\u2adc" * 250],
            # a NOTE of many short CONT lines, which the clip packs in
            [
                "1 BIRT",
                "2 DATE 7 JAN 1919",
                "2 NOTE Issued in 1938.",
                *["3 CONT For the emigration."] * 40,
            ],
        ],
        ids=["long-line", "longer-in-NFC", "many-lines"],
    )
    def test_every_excerpt_fits(self, lines):
        batch = adapt_gedcom(gustavs_births(lines), artifact=EXPORT).batch
        assert batch.claims
        for claim in batch.claims:
            excerpt = claim.source.transcription_excerpt.text
            assert len(unicodedata.normalize("NFC", excerpt)) <= 400
            assert "\n" not in excerpt

    def test_each_line_of_a_continued_note_is_a_line_of_the_excerpt(self):
        # (here) the export's NOTE on Gustav's birth runs on over CONT
        # lines: each is joined as a line of its own, and a clip keeps the
        # first of them that fit
        short = ["1 BIRT", "2 DATE 7 JAN 1919", "2 NOTE Issued in 1938."]
        short.append("3 CONT For the emigration.")
        batch = adapt_gedcom(gustavs_births(short), artifact=EXPORT).batch
        (birth,) = claims_for(batch, "i8", "date_of_birth")
        assert birth.source.transcription_excerpt.text == (
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 NOTE Issued in 1938."
            " | For the emigration."
        )
        sentence = note_of(60)[len("2 NOTE ") :]
        long = ["1 BIRT", "2 DATE 7 JAN 1919", "2 NOTE Issued in 1938."]
        long += [f"3 CONT {sentence}"] * 6 + ["2 SOUR @S1@"]
        batch = adapt_gedcom(gustavs_births(long), artifact=EXPORT).batch
        (birth,) = claims_for(batch, "i8", "date_of_birth")
        excerpt = birth.source.transcription_excerpt.text
        assert excerpt.startswith(
            "1 BIRT | 2 DATE 7 JAN 1919 | 2 NOTE Issued in 1938."
            f" | {sentence} | "
        )
        assert f" | … | 2 SOUR @S1@ | cited: {BIRTH_EXTRACT} | " in excerpt
        assert "\n" not in excerpt and len(excerpt) <= 400

    def test_an_excerpt_is_in_the_form_the_instance_stores(self):
        # (here) the export spells Flörburg, and the extract's title, in
        # decomposed form: the excerpt is in NFC, as the instance stores
        # text, and its length is the stored one's
        place = unicodedata.normalize("NFD", FLOERBURG)
        title = unicodedata.normalize("NFD", BIRTH_EXTRACT)
        text = f"0 @S1@ SOUR\n1 TITL {title}\n" + person(
            "I8",
            "Gustav /Löbig/",
            "1 BIRT",
            "2 DATE 7 JAN 1919",
            f"2 PLAC {place}",
            "2 SOUR @S1@",
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        (birth,) = claims_for(batch, "i8", "date_of_birth")
        assert birth.source.transcription_excerpt.text == (
            f"1 BIRT | 2 DATE 7 JAN 1919 | 2 PLAC {FLOERBURG} | 2 SOUR @S1@"
            f" | cited: {BIRTH_EXTRACT}"
        )

    def test_a_line_after_one_left_out_is_kept_where_it_fits(self):
        # Gustav was born on 7 January 1919 and on 1 July 1919: (here) one
        # BIRT gives both dates, a long NOTE between them, and cites the
        # tree. The NOTE is left out, and the second date after it kept
        lines = ["1 BIRT", "2 DATE 7 JAN 1919", note_of(450)]
        lines += ["2 DATE 1 JUL 1919", "2 SOUR @S2@"]
        batch = adapt_gedcom(gustavs_births(lines), artifact=EXPORT).batch
        whole = " | ".join(lines) + f" | cited: {TREE}"
        for birth in claims_for(batch, "i8", "date_of_birth"):
            assert birth.source.transcription_excerpt.text == (
                "1 BIRT | 2 DATE 7 JAN 1919 | … | 2 DATE 1 JUL 1919"
                f" | 2 SOUR @S2@ | cited: {TREE}" + clipped(whole)
            )

    def test_a_citation_keeps_the_line_it_stands_beneath(self):
        # (here) the family record's HUSB line carries a NOTE of each length
        # up to 330 characters, and its DIV cites the judgment, with a long
        # NOTE after the citation: the clip keeps the DIV line the
        # citation stands beneath before any other line it could keep, so
        # the citation is never read as the HUSB's
        for length in range(1, 331):
            text = cited(JUDGMENT) + leopold_and_agnes(
                "1 HUSB @I11@",
                f"2 NOTE {text_of(length)}",
                "1 WIFE @I17@",
                "1 DIV",
                "2 DATE 2 JAN 1957",
                "2 SOUR @S1@",
                "3 PAGE Bl. 1",
                f"2 NOTE {text_of(300)}",
            )
            ((_, excerpt),) = sources(text, "f5", "ended_by")
            lines = excerpt.split(" | ")
            assert "1 DIV" in lines, length
            assert lines.index("1 DIV") < lines.index("2 SOUR @S1@"), length
            assert len(excerpt) <= 400, length

    def test_an_excerpt_of_400_characters_is_whole(self):
        # (here) a NOTE on Gustav's birth brings its excerpt to exactly 400
        # characters, which it keeps whole, and then to 401, which it clips
        head = ["1 BIRT", "2 DATE 7 JAN 1919"]
        cited_part = f" | 2 SOUR @S1@ | cited: {BIRTH_EXTRACT}"
        fill = 400 - len(" | ".join(head)) - len(cited_part) - len(" | ")
        for length, clips in ((fill, False), (fill + 1, True)):
            note = f"2 NOTE {text_of(length - len('2 NOTE '))}"
            lines = [*head, note, "2 SOUR @S1@"]
            batch = adapt_gedcom(gustavs_births(lines), artifact=EXPORT).batch
            (birth,) = claims_for(batch, "i8", "date_of_birth")
            whole = " | ".join(lines) + f" | cited: {BIRTH_EXTRACT}"
            assert len(whole) == length - fill + 400
            excerpt = birth.source.transcription_excerpt.text
            assert (excerpt == whole) is not clips
            assert excerpt.endswith(clipped(whole)) is clips


class TestIngestGedcom:
    """`yizkr ingest gedcom` on a copy of the fixture under another name."""

    @staticmethod
    def run(tmp_path, monkeypatch, never_ingest: str = ""):
        """(the command's status, the batch file it was to write)."""
        from yizkr.cli import build_parser

        never = tmp_path / "never-ingest.txt"
        never.write_text(never_ingest)
        monkeypatch.setenv("YIZKR_EXCLUDE_FILE", str(never))
        export = tmp_path / "another-name.ged"
        export.write_bytes(FIXTURE.read_bytes())
        out = tmp_path / "batch.json"
        args = build_parser().parse_args(
            ["ingest", "gedcom", "--file", str(export), "--out", str(out)]
        )
        return args.func(args), out

    def test_the_claims_cite_the_file_by_its_bytes(
        self, tmp_path, monkeypatch
    ):
        """It cites the export it reads by the SHA-1 of its bytes, whatever
        the file is called; it takes no QID (#102)."""
        status, out = self.run(tmp_path, monkeypatch)
        assert status == 0
        batch = ClaimBatch.model_validate_json(out.read_text())
        assert batch.claims
        assert all(claim.source.artifact == EXPORT for claim in batch.claims)

    def test_a_file_on_the_never_ingest_list_is_refused(
        self, tmp_path, monkeypatch
    ):
        """A listed export is refused before it is read: no batch."""
        with pytest.raises(SystemExit, match="never-ingest"):
            self.run(tmp_path, monkeypatch, never_ingest=f"{EXPORT.sha1}\n")
        assert not (tmp_path / "batch.json").exists()


def names_of_batch(text: str) -> dict[str, tuple[str, list[str]]]:
    """Each entity of the batch: id -> (label, aliases)."""
    batch = adapt_gedcom(text, artifact=EXPORT).batch
    return {e.id: (e.label, e.aliases) for e in batch.entities}


class TestABracketAlone:
    """A person whose only NAME is a bracketed part keeps an entity: its
    label is the bracket as written, its unsplit form the bracket's
    content, the part is counted, and its claims stay (ADR-0035 §6). The
    content as an alias gives no part the label lacks, so the alias
    cleanup drops it as derivable (§8)."""

    @pytest.mark.parametrize(
        ("name", "label"),
        [
            ("(Peppi)", "(Peppi)"),
            ("/(Peppi)/", "(Peppi)"),
            ("(Peppi) //", "(Peppi)"),
        ],
    )
    def test_a_bracket_alone_is_a_person(self, name, label):
        text = person("I7", name, "1 BIRT", "2 DATE 17 APR 1881")
        result = adapt_gedcom(text, artifact=EXPORT)
        (ida,) = result.batch.entities
        assert (ida.id, ida.label, ida.aliases) == ("i7", label, [])
        assert parts(ida.structure) == {"unsplit": ["Peppi"]}
        assert structure_problems(ida) == []
        assert result.outcomes["NAME bracketed part"] == 1
        assert stated(text, "i7", "date_of_birth") == [
            ((1881, 4, 17, "day"), [])
        ]
        assert not [w for w in result.warnings if "skipped" in w]

    def test_a_bracket_alone_takes_part_in_a_union(self):
        text = family("1 MARR", "2 DATE 8 FEB 1947").replace(
            "1 NAME Agnes /Löbig/", "1 NAME (Agnes)"
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert unions(batch) == {"f5": "marriage", "f5-wedding": "wedding"}
        assert sorted(v for _, v, _ in on(batch, "f5", "participant")) == [
            "i11",
            "i17",
        ]


class TestPartsBesideTheValue:
    """An NPFX or an NSFX the value does not restate reaches its field
    beside the value's name, and the full form is an alias, counted as
    `NAME parts not restated`; `NAME part without a name` is for a NAME
    with no value (ADR-0035 §6)."""

    def test_an_unrestated_prefix_is_a_title(self):
        text = person("I1", "Eugen /Löbig/", "2 NPFX Dr.")
        eugen_entity, outcomes = record_of(text, "i1")
        assert parts(eugen_entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "other")],
            "titles": ["Dr."],
        }
        assert eugen_entity.label == "Eugen Löbig"
        assert eugen_entity.aliases == ["Dr. Eugen Löbig"]
        assert outcomes == {
            "HEAD.LANG absent: und": 1,
            "NAME parts not restated": 1,
        }
        assert structure_problems(eugen_entity) == []

    def test_an_unrestated_suffix_is_a_suffix(self):
        text = person("I16", "Ursula /Löbig/", "2 NSFX Wwe.")
        ursula, outcomes = record_of(text, "i16")
        assert parts(ursula.structure)["suffixes"] == ["Wwe."]
        assert ursula.aliases == ["Ursula Löbig Wwe."]
        assert outcomes["NAME parts not restated"] == 1
        assert "NAME part without a name" not in outcomes

    def test_a_prefix_and_a_suffix_make_one_form(self):
        text = person("I16", "Ursula /Löbig/", "2 NPFX Frau", "2 NSFX Wwe.")
        ursula, outcomes = record_of(text, "i16")
        assert ursula.aliases == ["Frau Ursula Löbig Wwe."]
        assert parts(ursula.structure)["titles"] == ["Frau"]
        assert outcomes["NAME parts not restated"] == 1

    def test_a_restated_prefix_is_a_title_and_no_form(self):
        text = person("I9", "Fräulein Klara /Löbig/", "2 NPFX Fräulein")
        klara, outcomes = record_of(text, "i9")
        assert parts(klara.structure)["titles"] == ["Fräulein"]
        assert klara.aliases == []
        assert "NAME parts not restated" not in outcomes

    def test_a_name_with_no_value_counts_its_lone_prefix(self):
        # beside Eugen's own NAME, a NAME with no value whose parts give
        # only a prefix names no one
        text = person("I1", "Eugen /Löbig/", "1 NAME", "2 NPFX Dr.")
        eugen_entity, outcomes = record_of(text, "i1")
        assert "titles" not in parts(eugen_entity.structure)
        assert outcomes["NAME part without a name"] == 1
        assert "NAME parts not restated" not in outcomes

    def test_a_suffix_beside_given_name_and_surname_parts(self):
        text = person(
            "I16",
            "Ursula /Löbig/ Wwe.",
            "2 GIVN Ursula",
            "2 SURN Löbig",
            "2 NSFX Wwe.",
        )
        ursula, outcomes = record_of(text, "i16")
        assert parts(ursula.structure) == {
            "given": ["Ursula"],
            "surnames": [("Löbig", "other")],
            "suffixes": ["Wwe."],
        }
        assert "NAME parts not restated" not in outcomes

    def test_a_suffix_part_takes_the_place_of_the_values(self):
        # the NSFX gives the suffixes, so the value's own is not a second
        text = person("I16", "Ursula /Löbig/ Wwe.", "2 NSFX Wwe")
        ursula, _ = record_of(text, "i16")
        assert parts(ursula.structure)["suffixes"] == ["Wwe"]

    def test_a_given_name_part_gives_each_word(self):
        text = person(
            "I12", "", "2 GIVN Rudolf Felix", "2 SURN Löbig"
        ).replace("1 NAME \n", "1 NAME\n")
        rudolf_entity, _ = record_of(text, "i12")
        assert parts(rudolf_entity.structure)["given"] == ["Rudolf", "Felix"]

    def test_a_particle_the_surname_part_holds_is_not_doubled(self):
        text = person(
            "I18", "S. /van 't Nijks/", "2 SPFX van 't", "2 SURN van 't Nijks"
        )
        entity, outcomes = record_of(text, "i18")
        assert parts(entity.structure)["surnames"] == [
            ("van 't Nijks", "other")
        ]
        assert "SPFX without a surname" not in outcomes

    def test_a_particle_the_values_surname_holds_is_not_counted(self):
        text = person("I1", "Eugen /de Löbig/", "2 SPFX de")
        _, outcomes = record_of(text, "i1")
        assert "SPFX without a surname" not in outcomes

    def test_a_lone_particle_is_counted_once(self):
        text = person("I1", "Eugen /Löbig/", "1 NAME", "2 SPFX de")
        _, outcomes = record_of(text, "i1")
        assert outcomes["SPFX without a surname"] == 1
        assert "NAME part without a name" not in outcomes

    def test_a_prefix_of_several_words_is_matched_whole(self):
        text = person("I7", "Frau Dr. Ida /Löbig/", "2 NPFX Frau Dr.")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["given"] == ["Ida"]
        assert parts(ida.structure)["titles"] == ["Frau Dr."]
        # a value that starts with its first word alone keeps its words
        text = person("I7", "Frau Ida /Löbig/", "2 NPFX Frau Dr.")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["given"] == ["Frau", "Ida"]

    def test_a_prefix_the_value_does_not_start_with_stays(self):
        # only a prefix the given part starts with is taken from it
        text = person("I7", "Ida Frau /Löbig/", "2 NPFX Frau")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["given"] == ["Ida", "Frau"]

    @pytest.mark.parametrize("kind", ["Birth", "BIRTH", " birth "])
    def test_a_name_type_is_read_in_any_case(self, kind):
        text = person("I1", "Eugen /Löbig/", f"2 TYPE {kind}")
        eugen_entity, _ = record_of(text, "i1")
        assert parts(eugen_entity.structure)["surnames"] == [
            ("Löbig", "birth")
        ]
        assert stated(text, "i1", "birth_name") == [("Eugen Löbig", [])]


class TestQuotesAndSlashes:
    """The quote rule's forms, a quoted NICK, and the counts of a NAME
    whose slashes do not give one surname (ADR-0035 §6)."""

    @pytest.mark.parametrize(
        "name",
        [
            "Gustav 'Gustl' /Löbig/",
            'Gustav "Gustl" /Löbig/',
            "Gustav “Gustl” /Löbig/",
            "Gustav ‘Gustl’ /Löbig/",
        ],
    )
    def test_each_quote_marks_a_nickname(self, name):
        gustav_entity, _ = record_of(person("I8", name), "i8")
        assert parts(gustav_entity.structure)["nicknames"] == ["Gustl"]
        assert parts(gustav_entity.structure)["given"] == ["Gustav"]

    def test_an_apostrophe_within_a_word_opens_no_quote(self):
        # though a later word ends with one: a quote opens a word
        entity, _ = record_of(person("I18", "S. van't Nijks' /Tarle/"), "i18")
        assert "nicknames" not in parts(entity.structure)

    def test_an_apostrophe_opening_a_word_is_no_quote(self):
        entity, _ = record_of(person("I18", "S. van 't /Nijks/"), "i18")
        assert "nicknames" not in parts(entity.structure)
        assert parts(entity.structure)["given"] == ["S.", "van", "'t"]

    @pytest.mark.parametrize(
        "nick", ["'Cilli'", '"Cilli"', "“Cilli”", "‘Cilli’", "' Cilli '"]
    )
    def test_a_quoted_nick_loses_its_quotes(self, nick):
        text = person("I2", "Cäcilie 'Cilli' /Tarle/", f"2 NICK {nick}")
        caecilie, _ = record_of(text, "i2")
        assert parts(caecilie.structure)["nicknames"] == ["Cilli"]
        assert stated(text, "i2", "nickname") == [("Cilli", [])]

    def test_a_nick_in_mixed_quotes_loses_them(self):
        # as a NAME value's quote does: any opening and any closing mark
        text = person("I2", "Cäcilie /Tarle/", "2 NICK 'Cilli”")
        caecilie, _ = record_of(text, "i2")
        assert parts(caecilie.structure)["nicknames"] == ["Cilli"]

    def test_a_name_with_two_pairs_of_slashes_is_unsplit(self):
        text = person("I2", "Cäcilie /Löbig/ /Tarle/")
        caecilie, outcomes = record_of(text, "i2")
        assert parts(caecilie.structure) == {
            "unsplit": ["Cäcilie Löbig Tarle"]
        }
        assert outcomes["NAME slashes not one pair"] == 1
        assert "NAME without slashes" not in outcomes

    @pytest.mark.parametrize(
        ("lines", "expected"),
        [
            (
                ["2 SURN Löbig"],
                {"surnames": [("Löbig", "other")]},
            ),
            (
                ["2 GIVN Rudolf"],
                {"given": ["Rudolf"]},
            ),
        ],
    )
    def test_a_name_without_slashes_and_one_part_stays_unsplit(
        self, lines, expected
    ):
        # the part gives its own field, and the value, whose other words
        # it does not place, is an unsplit form beside it
        text = person("I12", "Rudolf Löbig", *lines)
        rudolf_entity, outcomes = record_of(text, "i12")
        assert parts(rudolf_entity.structure) == {
            **expected,
            "unsplit": ["Rudolf Löbig"],
        }
        assert outcomes["NAME without slashes, one part given"] == 1
        assert structure_problems(rudolf_entity) == []

    def test_a_name_without_slashes_and_both_parts_is_split(self):
        text = person("I12", "Rudolf Löbig", "2 GIVN Rudolf", "2 SURN Löbig")
        rudolf_entity, outcomes = record_of(text, "i12")
        assert "unsplit" not in parts(rudolf_entity.structure)
        assert not [k for k in outcomes if k.startswith("NAME without")]

    def test_a_married_name_tag_with_no_surname_is_unsplit(self):
        text = person("I2", "Cäcilie /Tarle/", "1 _MARNM Cäcilie //")
        caecilie, outcomes = record_of(text, "i2")
        assert parts(caecilie.structure)["unsplit"] == ["Cäcilie"]
        assert outcomes["_MARNM without a surname"] == 1
        assert stated(text, "i2", "married_name") == []

    def test_a_married_name_tag_with_a_surname_is_a_married_name(self):
        text = person("I2", "Cäcilie /Tarle/", "1 _MARNM Cäcilie /Löbig/")
        assert stated(text, "i2", "married_name") == [("Cäcilie Löbig", [])]


class TestAliasHygiene:
    def test_a_form_that_carries_nothing_new_is_dropped(self):
        # (here) Ursula's second NAME adds no part her first does not hold
        text = person("I16", "Ursula Maria /Löbig/", "1 NAME Maria /Löbig/")
        ursula, _ = record_of(text, "i16")
        assert ursula.aliases == []

    @pytest.mark.parametrize(
        ("first", "second", "alias"),
        [
            ("Gustav /Löbig/", "Gustav /Lobig/", "Gustav Lobig"),
            ("Cäcilie /Tarle/", "Cacilie /Tarle/", "Cacilie Tarle"),
            ("Eugen /Fläckfeld/", "Eugen /Flackfeld/", "Eugen Flackfeld"),
            ("Ida /Sörne/", "Ida /Sorne/", "Ida Sorne"),
            ("Hans-Xxx /Löbig/", "Hans Xxx /Löbig/", "Hans Xxx Löbig"),
        ],
    )
    def test_an_untyped_spelling_variant_stays(self, first, second, alias):
        """A second, untyped NAME that spells the label's parts otherwise
        (an umlaut written plain, a hyphen as a space) gives no claim, so
        the alias is the one place its spelling lands: a spelling variant
        is never derivable (ADR-0035 §8)."""
        found, _ = record_of(person("I8", first, f"1 NAME {second}"), "i8")
        assert found.aliases == [alias]

    def test_the_cleanup_reads_the_record(self):
        """The adapter cleans a person's aliases by its record (ADR-0035
        §8): Gustav's second NAME 'Gustav /Löbig/' is derivable and goes,
        while his NICK 'Gustl', a part the label lacks, stays, and so does
        'Herr Löbig', the only form holding his NPFX title."""
        text = person(
            "I8",
            "Gustav /Löbig/",
            "2 NICK Gustl",
            "1 NAME Gustav /Löbig/",
            "1 NAME Herr /Löbig/",
            "2 NPFX Herr",
        )
        gustav, _ = record_of(text, "i8")
        assert "Gustl" in gustav.aliases
        assert "Gustav Löbig" not in gustav.aliases
        assert parts(gustav.structure)["titles"] == ["Herr"]
        assert [a for a in gustav.aliases if "Herr" in a] == ["Herr Löbig"]


class TestTheFixturesNicknames:
    def test_gustls_nick_is_a_nickname_of_gustavs_record(self):
        # the clustering stage (#409) meets the postcard's "Gustl" with
        # the export's Gustav on this nickname
        gustav_entity = next(
            e for e in fixture_result().batch.entities if e.id == "i8"
        )
        assert "Gustl" in gustav_entity.structure.nicknames
        assert "Gustl" in gustav_entity.aliases
        assert ("Gustl", []) in stated(FIXTURE.read_text(), "i8", "nickname")


class TestUnionShapes:
    """The kinds, labels, ids and counts of the unions a family gives
    (ADR-0036 §5)."""

    @pytest.mark.parametrize(
        ("kind", "wedding"),
        [
            ("civil", "civil_wedding"),
            ("civil ceremony", "civil_wedding"),
            ("civil marriage", "civil_wedding"),
            ("Standesamt", "civil_wedding"),
            ("standesamtlich", "civil_wedding"),
            ("religious", "religious_wedding"),
            ("religious ceremony", "religious_wedding"),
            ("religious marriage", "religious_wedding"),
            ("church", "religious_wedding"),
            ("church wedding", "religious_wedding"),
            ("kirchlich", "religious_wedding"),
            ("  Civil   Marriage ", "civil_wedding"),
            ("KIRCHLICH", "religious_wedding"),
        ],
    )
    def test_each_wedding_type_gives_its_kind(self, kind, wedding):
        result = adapt_gedcom(
            family("1 MARR", f"2 TYPE {kind}", "2 DATE 8 FEB 1947"),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {
            "f5": "marriage",
            f"f5-{wedding.replace('_', '-')}": wedding,
        }
        assert result.unsupported_tags == {}

    def test_the_wedding_types_are_those_pinned_above(self):
        assert set(WEDDING_TYPES) == {
            "civil", "civil ceremony", "civil marriage", "standesamt",
            "standesamtlich", "religious", "religious ceremony",
            "religious marriage", "church", "church wedding", "kirchlich",
        }  # fmt: skip

    def test_types_of_one_kind_make_one_wedding(self):
        result = adapt_gedcom(
            family(
                "1 MARR", "2 TYPE civil", "2 DATE 8 FEB 1947",
                "1 MARR", "2 TYPE Standesamt", "2 DATE 1947",
            ),
            artifact=EXPORT,
        )  # fmt: skip
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-civil-wedding": "civil_wedding",
        }

    def test_a_type_of_no_known_kind_joins_the_unstated_wedding(self):
        result = adapt_gedcom(
            family(
                "1 MARR", "2 TYPE Elopement", "2 DATE 8 FEB 1947",
                "1 MARR", "2 DATE 1947",
            ),
            artifact=EXPORT,
        )  # fmt: skip
        assert unions(result.batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
        }
        assert len(on(result.batch, "f5-wedding", "point_in_time")) == 2
        assert result.unsupported_tags == {"MARR.TYPE": 1}

    def test_each_event_has_its_label(self):
        text = family(
            "1 MARR", "2 TYPE civil", "2 DATE 8 FEB 1947",
            "1 MARR", "2 TYPE religious", "2 DATE 9 FEB 1947",
            "1 ANUL", "2 DATE 1948",
            "1 DIV", "2 DATE 2 JAN 1957",
        )  # fmt: skip
        names = "Leopold Löbig and Agnes Löbig"
        assert {k: v[0] for k, v in names_of_batch(text).items()} == {
            "i11": "Leopold Löbig",
            "i17": "Agnes Löbig",
            "f5": f"Marriage of {names}",
            "f5-civil-wedding": f"Civil wedding of {names}",
            "f5-religious-wedding": f"Religious wedding of {names}",
            "f5-annulment": f"Annulment of {names}",
            "f5-divorce": f"Divorce of {names}",
        }

    @pytest.mark.parametrize(
        ("lines", "key"),
        [
            (["1 DIV", "2 DATE sometime"], "union: undated divorce"),
            (["1 DIV Y", "2 DATE"], "union: undated divorce"),
            (["1 ANUL Y"], "union: undated annulment"),
            (["1 ANUL", "2 PLAC Flörburg"], "union: undated annulment"),
        ],
    )
    def test_an_end_whose_dates_give_no_date_is_counted_undated(
        self, lines, key
    ):
        result = adapt_gedcom(family("1 MARR Y", *lines), artifact=EXPORT)
        assert result.outcomes[key] == 1

    def test_a_dated_end_is_not_counted_undated(self):
        result = adapt_gedcom(
            family("1 MARR Y", "1 DIV", "2 DATE 2 JAN 1957"), artifact=EXPORT
        )
        assert "union: undated divorce" not in result.outcomes

    @pytest.mark.parametrize("tag", ["MARR Y", "DIV Y"])
    def test_an_empty_date_warns_nothing(self, tag):
        result = adapt_gedcom(family(f"1 {tag}", "2 DATE"), artifact=EXPORT)
        assert result.warnings == []

    def test_a_family_whose_id_is_another_unions_event_has_its_own(self):
        text = family("1 MARR", "2 DATE 8 FEB 1947").replace(
            "0 TRLR\n",
            "0 @F5-WEDDING@ FAM\n1 HUSB @I11@\n1 WIFE @I17@\n0 TRLR\n",
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert unions(batch) == {
            "f5": "marriage",
            "f5-wedding": "wedding",
            "f5-wedding-2": "partnership",
        }
        assert on(batch, "f5-wedding", "participant") == []
        assert len(on(batch, "f5-wedding-2", "participant")) == 2

    def test_a_family_whose_id_is_a_persons_has_its_own(self):
        text = family("1 MARR Y").replace("0 @F5@ FAM", "0 @I11@ FAM")
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert unions(batch) == {
            "i11-2": "marriage",
            "i11-2-wedding": "wedding",
        }
        assert on(batch, "i11", "participant") == []


def same_tag_family(*lines: str) -> str:
    """Rudolf and Leopold's family record (here), of `lines`, with Agnes
    and Eugen in the batch too."""
    return "\n".join(
        [
            "0 @I1@ INDI",
            "1 NAME Eugen /Löbig/",
            "0 @I11@ INDI",
            "1 NAME Leopold /Löbig/",
            "0 @I12@ INDI",
            "1 NAME Rudolf /Löbig/",
            "0 @I17@ INDI",
            "1 NAME Agnes /Löbig/",
            "0 @F9@ FAM",
            *lines,
            "0 TRLR",
            "",
        ]
    )


def parents_of(batch, child: str) -> list[tuple[str, str]]:
    """The parent claims on `child`, as sorted (property, parent) pairs."""
    return sorted(
        (prop, value)
        for prop in ("father", "mother")
        for _, value, _ in on(batch, child, prop)
    )


def couple_of(batch, union: str = "f9") -> list[str]:
    """The participants of `union`, sorted."""
    return sorted(value for _, value, _ in on(batch, union, "participant"))


class TestTwoOfOneTag:
    """A family's first two HUSB and WIFE lines, in either combination,
    are its couple, and each is its children's parent as its tag states:
    two HUSBs are two fathers, two WIFEs two mothers. A couple of one tag
    is counted, and a further partner line is counted, not read (ADR-0036
    §5, #353)."""

    @pytest.mark.parametrize("tag", ["HUSB", "WIFE"])
    def test_two_of_one_tag_are_a_partnership(self, tag):
        result = adapt_gedcom(
            same_tag_family(f"1 {tag} @I11@", f"1 {tag} @I12@"),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {"f9": "partnership"}
        assert on(result.batch, "f9", "participant") == [
            ("participant", "i11", [("object_has_role", "partner")]),
            ("participant", "i12", [("object_has_role", "partner")]),
        ]
        assert result.outcomes["FAM two HUSB/WIFE"] == 1
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        ("tag", "prop"), [("HUSB", "father"), ("WIFE", "mother")]
    )
    def test_each_of_two_of_one_tag_is_the_childrens_parent(self, tag, prop):
        # (here) Leopold and Rudolf, as two HUSBs or two WIFEs, give Eugen
        # two fathers or two mothers, each claim citing the family record
        text = same_tag_family(
            f"1 {tag} @I11@", f"1 {tag} @I12@", "1 CHIL @I1@", "1 MARR Y"
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert parents_of(result.batch, "i1") == [
            (prop, "i11"),
            (prop, "i12"),
        ]
        for parent in claims_for(result.batch, "i1", prop):
            assert parent.source.transcription_excerpt.text == (
                f"0 @F9@ FAM | 1 {tag} @I11@ | 1 {tag} @I12@"
                " | 1 CHIL @I1@ | 1 MARR Y"
            )
        assert unions(result.batch) == {
            "f9": "marriage",
            "f9-wedding": "wedding",
        }
        assert couple_of(result.batch) == ["i11", "i12"]
        assert result.outcomes["FAM two HUSB/WIFE"] == 1
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        "lines",
        [
            ["1 HUSB @I11@", "1 WIFE @I17@"],
            ["1 WIFE @I17@", "1 HUSB @I11@"],
        ],
        ids=["husband-first", "wife-first"],
    )
    def test_a_husband_and_a_wife_are_a_father_and_a_mother(self, lines):
        result = adapt_gedcom(
            same_tag_family(*lines, "1 CHIL @I1@"), artifact=EXPORT
        )
        assert parents_of(result.batch, "i1") == [
            ("father", "i11"),
            ("mother", "i17"),
        ]
        assert couple_of(result.batch) == ["i11", "i17"]
        assert "FAM two HUSB/WIFE" not in result.outcomes
        assert result.unsupported_tags == {}

    def test_two_of_one_tag_with_a_marriage(self):
        result = adapt_gedcom(
            same_tag_family(
                "1 HUSB @I11@", "1 HUSB @I12@", "1 MARR", "2 DATE 1958"
            ),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {
            "f9": "marriage",
            "f9-wedding": "wedding",
        }
        assert result.unsupported_tags == {}

    @pytest.mark.parametrize(
        ("lines", "couple", "parents", "counted"),
        [
            (
                ["1 HUSB @I11@", "1 HUSB @I12@", "1 HUSB @I17@"],
                ["i11", "i12"],
                [("father", "i11"), ("father", "i12")],
                {"FAM.HUSB": 1},
            ),
            (
                ["1 HUSB @I11@", "1 HUSB @I12@", "1 WIFE @I17@"],
                ["i11", "i12"],
                [("father", "i11"), ("father", "i12")],
                {"FAM.WIFE": 1},
            ),
            (
                ["1 WIFE @I17@", "1 HUSB @I11@", "1 HUSB @I12@"],
                ["i11", "i17"],
                [("father", "i11"), ("mother", "i17")],
                {"FAM.HUSB": 1},
            ),
        ],
        ids=["three-husbands", "a-wife-third", "a-husband-third"],
    )
    def test_the_first_two_are_the_couple_and_a_third_is_counted(
        self, lines, couple, parents, counted
    ):
        result = adapt_gedcom(
            same_tag_family(*lines, "1 CHIL @I1@"), artifact=EXPORT
        )
        assert couple_of(result.batch) == couple
        assert parents_of(result.batch, "i1") == parents
        assert result.unsupported_tags == counted

    def test_a_wife_written_first_is_read_after_the_husband(self):
        # a HUSB and a WIFE are read husband first, whatever their order:
        # the union's label, its excerpts and the claims' order are those
        # of a family that writes the HUSB first
        result = adapt_gedcom(
            same_tag_family(
                "1 WIFE @I17@", "1 HUSB @I11@", "1 CHIL @I1@", "1 MARR Y"
            ),
            artifact=EXPORT,
        )
        (union,) = [e for e in result.batch.entities if e.id == "f9"]
        assert union.label == "Marriage of Leopold Löbig and Agnes Löbig"
        for participant in claims_for(result.batch, "f9", "participant"):
            assert participant.source.transcription_excerpt.text.startswith(
                "0 @F9@ FAM | 1 HUSB @I11@ | 1 WIFE @I17@"
            )
        assert [
            c.property
            for c in claims_for(result.batch, "i1")
            if c.property in ("father", "mother")
        ] == ["father", "mother"]

    @pytest.mark.parametrize(
        ("lines", "counted"),
        [
            (["1 HUSB @I11@", "1 HUSB @I11@", "1 WIFE @I17@"], "FAM.HUSB"),
            (["1 WIFE @I17@", "1 WIFE @I17@", "1 HUSB @I11@"], "FAM.WIFE"),
        ],
        ids=["husband-restated", "wife-restated"],
    )
    def test_a_restated_line_gives_way_to_the_next(self, lines, counted):
        result = adapt_gedcom(
            same_tag_family(*lines, "1 CHIL @I1@"), artifact=EXPORT
        )
        assert couple_of(result.batch) == ["i11", "i17"]
        assert parents_of(result.batch, "i1") == [
            ("father", "i11"),
            ("mother", "i17"),
        ]
        assert result.unsupported_tags == {counted: 1}

    def test_partner_lines_that_name_no_one_are_no_one_person(self):
        # a HUSB and a WIFE with no pointer name no person, so they are
        # not one person as both
        result = adapt_gedcom(
            same_tag_family("1 HUSB", "1 WIFE", "1 CHIL @I1@"),
            artifact=EXPORT,
        )
        assert "FAM one person as HUSB and WIFE" not in result.outcomes
        assert parents_of(result.batch, "i1") == []

    def test_one_person_twice_is_no_couple(self):
        result = adapt_gedcom(
            same_tag_family(
                "1 HUSB @I11@", "1 HUSB @I11@", "1 CHIL @I1@", "1 MARR Y"
            ),
            artifact=EXPORT,
        )
        assert unions(result.batch) == {}
        assert parents_of(result.batch, "i1") == [("father", "i11")]
        assert "FAM two HUSB/WIFE" not in result.outcomes
        # the second restates the first and is counted, and the MARR,
        # which has no couple, with it
        assert result.unsupported_tags == {"FAM.HUSB": 1, "MARR": 1}

    def test_a_third_naming_a_partner_is_counted_not_refused(self):
        # (here) Leopold as a WIFE after Leopold and Rudolf as HUSBs: the
        # third is no partner line, so the family keeps its couple
        result = adapt_gedcom(
            same_tag_family(
                "1 HUSB @I11@", "1 HUSB @I12@", "1 WIFE @I11@", "1 CHIL @I1@"
            ),
            artifact=EXPORT,
        )
        assert couple_of(result.batch) == ["i11", "i12"]
        assert parents_of(result.batch, "i1") == [
            ("father", "i11"),
            ("father", "i12"),
        ]
        assert "FAM one person as HUSB and WIFE" not in result.outcomes
        assert result.unsupported_tags == {"FAM.WIFE": 1}

    def test_a_partner_listed_as_their_own_child_loses_that_link_alone(self):
        result = adapt_gedcom(
            same_tag_family(
                "1 WIFE @I11@", "1 WIFE @I12@", "1 CHIL @I12@", "1 CHIL @I1@"
            ),
            artifact=EXPORT,
        )
        assert parents_of(result.batch, "i12") == [("mother", "i11")]
        assert parents_of(result.batch, "i1") == [
            ("mother", "i11"),
            ("mother", "i12"),
        ]
        assert result.outcomes["FAM one person as parent and CHIL"] == 1
        assert couple_of(result.batch) == ["i11", "i12"]

    def test_a_partner_the_batch_lacks_gives_no_couple(self):
        # (here) Rudolf's record is missing: Leopold is still Eugen's
        # father, and the family has no couple, so its MARR is counted
        text = same_tag_family(
            "1 HUSB @I11@", "1 HUSB @I99@", "1 CHIL @I1@", "1 MARR Y"
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert unions(result.batch) == {}
        assert parents_of(result.batch, "i1") == [("father", "i11")]
        assert result.unsupported_tags == {"MARR": 1}


def leopold_and(name: str, *lines: str) -> str:
    """Leopold's family with Agnes, Agnes named (here) `name`."""
    return family("1 MARR", "2 DATE 8 FEB 1947").replace(
        "1 NAME Agnes /Löbig/", "\n".join([f"1 NAME {name}", *lines])
    )


SKIPPED = "@I17@: no NAME gives a name; skipped entirely, with the family "


class TestNamesWithoutWords:
    """A name or a name part that holds no letter or digit ('?', '()')
    names nothing: it is counted, never placed, and a person whose NAMEs
    give no word is skipped, as one with an empty NAME is (ADR-0035 §6)."""

    @pytest.mark.parametrize(
        ("name", "lines"),
        [
            ("()", []),
            ("( )", []),
            ("(?)", []),
            ("(,)", []),
            ("/()/", []),
            ("() //", []),
            ("()", ["2 NPFX Frau"]),
            ("?", []),
            ("-", []),
            ("…", []),
            ("", ["2 GIVN ?"]),
            ("_", []),
            ("___", []),
            ("/___/", []),
        ],
    )
    def test_a_name_without_a_word_skips_its_person(self, name, lines):
        result = adapt_gedcom(leopold_and(name, *lines), artifact=EXPORT)
        assert [e.id for e in result.batch.entities if e.kind == "human"] == [
            "i11"
        ]
        assert [w for w in result.warnings if w.startswith(SKIPPED)]
        assert result.outcomes["person without a name"] == 1
        # the rest of the export adapts: Leopold keeps his entity
        assert unions(result.batch) == {}

    def test_a_bracket_names_label_leaves_out_a_placeholder(self):
        entity, _ = record_of(person("I7", "(Peppi) ?"), "i7")
        assert entity.label == "(Peppi)"

    def test_brackets_without_a_word_beside_a_word(self):
        entity, outcomes = record_of(person("I7", "(Peppi) (?)"), "i7")
        assert (entity.label, entity.aliases) == ("(Peppi)", [])
        assert outcomes["NAME part without a word"] == 1
        entity, outcomes = record_of(person("I1", "Eugen (?) /Löbig/"), "i1")
        assert entity.label == "Eugen Löbig"
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize("bracket", ["()", "(Peppi)"])
    def test_brackets_give_the_label_only_where_nothing_else_does(
        self, bracket
    ):
        # an empty bracket, or a bracket that holds a name, listed first,
        # never takes the label from a NAME that gives one
        text = person("I7", bracket, "1 NAME Ida /Sörne/")
        ida, _ = record_of(text, "i7")
        assert ida.label == "Ida Sörne"
        assert ida.aliases == (["Peppi"] if bracket == "(Peppi)" else [])

    def test_two_people_named_by_empty_brackets_are_two(self):
        # neither is labelled '()', which the writer would make one item
        text = "\n".join(
            ["0 @I7@ INDI", "1 NAME ()", "1 NAME Ida /Sörne/",
             "0 @I17@ INDI", "1 NAME ()", "1 NAME Agnes /Löbig/",
             "0 TRLR", ""]
        )  # fmt: skip
        labels = {k: v[0] for k, v in names_of_batch(text).items()}
        assert labels == {"i7": "Ida Sörne", "i17": "Agnes Löbig"}

    @pytest.mark.parametrize("piece", ["?", ".", "†"])
    @pytest.mark.parametrize("tag", ["NPFX", "NSFX"])
    def test_a_prefix_or_suffix_without_a_word_is_counted(self, tag, piece):
        text = person("I1", "Eugen /Löbig/", f"2 {tag} {piece}")
        eugen_entity, outcomes = record_of(text, "i1")
        assert (eugen_entity.label, eugen_entity.aliases) == (
            "Eugen Löbig",
            [],
        )
        assert parts(eugen_entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "other")],
        }
        assert outcomes["NAME part without a word"] == 1
        assert "NAME parts not restated" not in outcomes

    def test_a_placeholder_value_beside_a_given_name_part(self):
        eugen_entity, outcomes = record_of(
            person("I1", "?", "2 GIVN Eugen"), "i1"
        )
        assert (eugen_entity.label, eugen_entity.aliases) == ("Eugen", [])
        assert parts(eugen_entity.structure) == {"given": ["Eugen"]}
        assert outcomes["NAME part without a word"] == 1

    def test_a_placeholder_surname_is_counted_not_placed(self):
        eugen_entity, outcomes = record_of(person("I1", "Eugen /?/"), "i1")
        assert eugen_entity.label == "Eugen"
        assert parts(eugen_entity.structure) == {"given": ["Eugen"]}
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize("nick", ["''", "?", '""', "_", "'___'", "' '"])
    def test_a_nick_without_a_word_is_counted(self, nick):
        text = person("I1", "Eugen /Löbig/", f"2 NICK {nick}")
        eugen_entity, outcomes = record_of(text, "i1")
        assert eugen_entity.aliases == []
        assert "nicknames" not in parts(eugen_entity.structure)
        assert outcomes["NAME part without a word"] == 1
        assert stated(text, "i1", "nickname") == []

    def test_a_quoted_part_without_a_word_is_counted(self):
        text = person("I1", "Eugen '?' /Löbig/")
        eugen_entity, outcomes = record_of(text, "i1")
        assert eugen_entity.label == "Eugen Löbig"
        assert outcomes["NAME part without a word"] == 1
        assert stated(text, "i1", "nickname") == []

    @pytest.mark.parametrize("tag", ["GIVN", "SURN"])
    def test_a_part_without_a_word_beside_the_value(self, tag):
        text = person("I1", "Eugen /Löbig/", f"2 {tag} ?")
        eugen_entity, outcomes = record_of(text, "i1")
        assert parts(eugen_entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "other")],
        }
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize(("marnm", "count"), [("/?/", 1), ("?", 1)])
    def test_a_married_name_without_a_word_is_counted(self, marnm, count):
        text = person("I17", "Agnes /Löbig/", f"1 _MARNM {marnm}")
        agnes, outcomes = record_of(text, "i17")
        assert agnes.aliases == []
        assert outcomes["NAME part without a word"] == count
        assert "_MARNM without a surname" not in outcomes
        assert stated(text, "i17", "married_name") == []

    def test_a_married_names_placeholder_is_left_out(self):
        text = person("I17", "Agnes /Löbig/", "1 _MARNM Agnes ?")
        agnes, _ = record_of(text, "i17")
        assert parts(agnes.structure)["unsplit"] == ["Agnes"]

    def test_a_name_whose_only_word_is_a_suffix_gives_no_record(self):
        # 'Jr.' alone names no one by a given name or a surname: the
        # entity stands, with no record, rather than one that fails
        entity, _ = record_of(person("I1", "// Jr."), "i1")
        assert entity.label == "Jr."
        assert entity.structure is None


class TestRound2Readings:
    """The small reading rules the orchestrator decided (decision 5)."""

    def test_a_quoted_word_after_the_surname_is_a_nickname(self):
        text = person("I1", "Eugen /Löbig/ 'Muckel'")
        eugen_entity, _ = record_of(text, "i1")
        assert parts(eugen_entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "other")],
            "nicknames": ["Muckel"],
        }
        assert stated(text, "i1", "nickname") == [("Muckel", [])]

    def test_a_married_names_brackets_are_left_out(self):
        text = person("I7", "Ida /Sörne/", "1 _MARNM (Peppi)")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["unsplit"] == ["Peppi"]
        assert ida.aliases == ["Peppi"]

    def test_a_given_name_parts_bracket_is_an_unsplit_form(self):
        # a bracket in a GIVN reads as one in the value does: it may hold
        # a maiden or a later name or a note as well as a nickname, so it
        # is an alias and an unsplit form, typed as nothing and counted
        text = person("I7", "Ida /Sörne/", "2 GIVN Ida (Peppi)")
        ida, outcomes = record_of(text, "i7")
        assert parts(ida.structure) == {
            "given": ["Ida"],
            "surnames": [("Sörne", "other")],
            "unsplit": ["Peppi"],
        }
        assert ida.aliases == ["Peppi"]
        assert outcomes["NAME bracketed part"] == 1
        assert stated(text, "i7", "nickname") == []
        assert structure_problems(ida) == []

    def test_a_given_name_parts_quote_is_a_nickname(self):
        text = person("I2", "Cäcilie /Tarle/", "2 GIVN Cäcilie 'Cilli'")
        caecilie, outcomes = record_of(text, "i2")
        assert "NAME bracketed part" not in outcomes
        assert parts(caecilie.structure)["given"] == ["Cäcilie"]
        assert parts(caecilie.structure)["nicknames"] == ["Cilli"]

    def test_undetermined_is_taken_as_it_is(self):
        result = adapt_gedcom(
            "0 HEAD\n1 LANG und\n" + eugen("1 BIRT", "2 DATE 1836"),
            artifact=EXPORT,
        )
        assert {
            c.source.transcription_excerpt.language
            for c in result.batch.claims
        } == {"und"}
        assert not [k for k in result.outcomes if k.startswith("HEAD.LANG")]

    def test_a_bracket_name_keeps_its_prefix(self):
        ida, outcomes = record_of(person("I7", "(Peppi)", "2 NPFX Frau"), "i7")
        assert parts(ida.structure) == {
            "titles": ["Frau"],
            "unsplit": ["Peppi"],
        }
        assert ida.aliases == ["Frau Peppi"]
        assert outcomes["NAME parts not restated"] == 1
        assert "NAME part without a name" not in outcomes
        assert structure_problems(ida) == []

    @pytest.mark.parametrize(
        ("name", "prefix"),
        [("Eugen /Löbig/", "Eugen"), ("Dr. /Löbig/", "Dr."),
         ("Eugen ? /Löbig/", "Eugen")],
    )  # fmt: skip
    def test_a_prefix_restated_as_the_whole_given_part_is_a_title(
        self, name, prefix
    ):
        # decision D: the export's own tag is taken at its word; telling a
        # mis-tagged given name from a title would need a word list
        entity, outcomes = record_of(
            person("I1", name, f"2 NPFX {prefix}"), "i1"
        )
        assert parts(entity.structure) == {
            "surnames": [("Löbig", "other")],
            "titles": [prefix],
        }
        assert outcomes["NPFX restated as the whole given part"] == 1
        assert outcomes.get("NAME part without a word", 0) == name.count("?")

    def test_a_prefix_restated_beside_given_name_and_surname_parts(self):
        text = person(
            "I1", "Eugen /Löbig/", "2 GIVN Eugen", "2 SURN Löbig",
            "2 NPFX Eugen",
        )  # fmt: skip
        entity, outcomes = record_of(text, "i1")
        assert parts(entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "other")],
            "titles": ["Eugen"],
        }
        assert outcomes["NPFX restated as the whole given part"] == 1

    def test_a_prefix_stated_in_part_is_not_written_twice(self):
        text = person("I1", "Dr. Eugen /Löbig/", "2 NPFX Dr. med.")
        eugen_entity, _ = record_of(text, "i1")
        assert eugen_entity.aliases == ["Dr. med. Eugen Löbig"]
        assert parts(eugen_entity.structure)["titles"] == ["Dr. med."]

    def test_a_prefix_matches_its_words_folded(self):
        text = person("I1", "Dr med Eugen /Löbig/", "2 NPFX Dr. med.")
        eugen_entity, outcomes = record_of(text, "i1")
        assert parts(eugen_entity.structure)["given"] == ["Eugen"]
        assert parts(eugen_entity.structure)["titles"] == ["Dr. med."]
        assert "NAME parts not restated" not in outcomes

    def test_a_restated_suffix_beside_the_value_gives_no_form(self):
        text = person("I1", "Eugen /Löbig/ Jr.", "2 NSFX Jr.")
        eugen_entity, outcomes = record_of(text, "i1")
        assert parts(eugen_entity.structure)["suffixes"] == ["Jr."]
        assert eugen_entity.aliases == []
        assert "NAME parts not restated" not in outcomes

    def test_a_married_name_with_a_blank_surname_is_unsplit(self):
        text = person("I17", "Agnes /Löbig/", "1 _MARNM Agnes / /")
        agnes, outcomes = record_of(text, "i17")
        assert parts(agnes.structure)["unsplit"] == ["Agnes"]
        assert outcomes["_MARNM without a surname"] == 1
        assert stated(text, "i17", "married_name") == []

    def test_a_slashless_name_with_one_part_keeps_its_typed_claim(self):
        text = person("I12", "Rudolf Löbig", "2 SURN Löbig", "2 TYPE birth")
        assert stated(text, "i12", "birth_name") == [("Rudolf Löbig", [])]

    def test_a_nick_of_two_quotes_names_nothing(self):
        text = person("I2", "Cäcilie /Tarle/", "2 NICK ''")
        caecilie, outcomes = record_of(text, "i2")
        assert "nicknames" not in parts(caecilie.structure)
        assert outcomes["NAME part without a word"] == 1


class TestRound2Unions:
    def test_a_dated_end_stays_dated_beside_an_unparsed_date(self):
        result = adapt_gedcom(
            family("1 MARR Y", "1 DIV", "2 DATE 1957", "2 DATE sometime"),
            artifact=EXPORT,
        )
        assert "union: undated divorce" not in result.outcomes

    def test_one_person_as_husband_and_wife_is_no_couple(self):
        text = family("1 MARR Y").replace("1 WIFE @I17@", "1 WIFE @I11@")
        result = adapt_gedcom(text, artifact=EXPORT)
        assert unions(result.batch) == {}

    def test_the_second_of_one_tag_is_a_parent_too(self):
        # decision 1, overridden by the owner (#353): a couple under one
        # tag are partners, and each is the child's parent as their tag
        # states, so two HUSBs are two fathers
        text = same_tag_family(
            "1 HUSB @I11@", "1 HUSB @I12@", "1 CHIL @I13@"
        ).replace(
            "0 @F9@ FAM", "0 @I13@ INDI\n1 NAME Gustav /Löbig/\n0 @F9@ FAM"
        )
        batch = adapt_gedcom(text, artifact=EXPORT).batch
        assert [v for _, v, _ in on(batch, "i13", "father")] == ["i11", "i12"]
        assert on(batch, "i13", "mother") == []
        assert unions(batch) == {"f9": "partnership"}

    @pytest.mark.parametrize(
        "lines",
        [
            ["1 DIV Y"],
            ["1 ANUL Y"],
            ["1 MARR N", "1 DIV", "2 DATE 2 JAN 1957"],
        ],
    )
    def test_an_end_with_no_wedding_is_of_a_marriage(self, lines):
        # decision 4: a divorce or an annulment ends a marriage, whose
        # wedding the export does not state
        result = adapt_gedcom(family(*lines), artifact=EXPORT)
        assert unions(result.batch)["f5"] == "marriage"
        assert not [k for k in unions(result.batch) if "wedding" in k]
        assert on(result.batch, "f5", "started_by") == []
        assert result.outcomes["union: marriage without a wedding"] == 1
        assert {
            role
            for _, _, quals in on(result.batch, "f5", "participant")
            for _, role in quals
        } == {"spouse"}

    def test_an_end_that_states_nothing_leaves_a_partnership(self):
        result = adapt_gedcom(family("1 DIV N"), artifact=EXPORT)
        assert unions(result.batch) == {"f5": "partnership"}
        assert "union: marriage without a wedding" not in result.outcomes


def wife_with_a_child(*agnes: str) -> str:
    """Eugen and Agnes's family with their son Leopold, Agnes's record of
    (here) `agnes`."""
    return "\n".join(
        [
            "0 @I1@ INDI", "1 NAME Eugen /Löbig/",
            "0 @I17@ INDI", *agnes, "1 BIRT", "2 DATE 1890",
            "0 @I11@ INDI", "1 NAME Leopold /Löbig/",
            "0 @F1@ FAM", "1 HUSB @I1@", "1 WIFE @I17@", "1 CHIL @I11@",
            "0 TRLR", "",
        ]
    )  # fmt: skip


class TestRound3Names:
    """A person with any word in any name keeps an entity (decision A),
    and a placeholder never takes a bracket or a quote with it."""

    @pytest.mark.parametrize(
        ("agnes", "label"),
        [
            # decision L: a fallback label keeps the source's marks
            (["1 NAME", "2 GIVN (Peppi)"], "(Peppi)"),
            (["1 NAME", "2 GIVN 'Peppi'"], "'Peppi'"),
            (["1 NAME //", '2 GIVN "Peppi"'], '"Peppi"'),
            (["1 NAME", "2 GIVN (Peppi)", "2 NPFX Frau"], "(Peppi)"),
            (["1 NAME ?", "2 NICK Muckel"], "Muckel"),
            (["1 NAME ?", "1 _MARNM Agnes /Löbig/"], "Agnes Löbig"),
        ],
    )
    def test_a_word_in_any_name_keeps_the_person(self, agnes, label):
        text = wife_with_a_child(*agnes)
        result = adapt_gedcom(text, artifact=EXPORT)
        labels = {e.id: e.label for e in result.batch.entities}
        assert labels["i17"] == label
        assert stated(text, "i17", "date_of_birth") == [(year(1890), [])]
        assert [v for _, v, _ in on(result.batch, "i11", "mother")] == ["i17"]
        assert unions(result.batch) == {"f1": "partnership"}
        assert not result.warnings
        assert (
            structure_problems(
                next(e for e in result.batch.entities if e.id == "i17")
            )
            == []
        )

    def test_the_tiers_keep_their_order(self):
        # a GIVN's bracket gives no label beside a NAME that gives one, and
        # comes before a NICK
        ida, _ = record_of(
            person("I7", "", "2 GIVN (Peppi)", "1 NAME Ida /Sörne/"), "i7"
        )
        assert ida.label == "Ida Sörne"
        assert ida.aliases == ["Peppi"]
        ida, _ = record_of(
            person("I7", "", "2 GIVN (Peppi)", "2 NICK Muckel"), "i7"
        )
        assert (ida.label, ida.aliases) == ("(Peppi)", ["Muckel"])

    @pytest.mark.parametrize(
        ("name", "lines", "label"),
        [
            ("(Peppi ?)", [], "(Peppi)"),
            ("(? Peppi)", [], "(Peppi)"),
            ("(? Peppi)", ["2 NPFX Frau"], "(Peppi)"),
            ("Eugen 'Muckel ?' /Löbig/", [], "Eugen 'Muckel' Löbig"),
            ("Eugen '? Muckel' /Löbig/", [], "Eugen 'Muckel' Löbig"),
        ],
    )
    def test_a_placeholder_leaves_its_brackets_and_quotes(
        self, name, lines, label
    ):
        text = person("I7", name, *lines)
        entity, outcomes = record_of(text, "i7")
        assert entity.label == label
        assert outcomes["NAME part without a word"] == 1
        assert structure_problems(entity) == []
        if lines:
            assert entity.aliases == ["Frau Peppi"]

    def test_a_bracket_of_brackets_is_read_by_its_outermost_pair(self):
        entity, outcomes = record_of(person("I7", "(Pep(pi))"), "i7")
        assert (entity.label, entity.aliases) == ("(Peppi)", [])
        assert outcomes["NAME bracketed part"] == 1
        entity, _ = record_of(person("I7", "((Peppi))"), "i7")
        assert entity.aliases == []

    def test_an_unclosed_bracket_reads_as_the_whole_value_does(self):
        entity, _ = record_of(person("I7", "Ida ( /Sörne (Peppi)/"), "i7")
        assert entity.label == "Ida Sörne"
        assert parts(entity.structure)["surnames"] == [("Sörne", "other")]

    def test_a_slashless_name_with_a_quoted_middle_word(self):
        # the form without the quote is a name form too, so the record
        # adds no name
        text = person("I1", "Eugen 'Muckel' Löbig")
        entity, _ = record_of(text, "i1")
        assert entity.label == "Eugen 'Muckel' Löbig"
        assert entity.aliases == ["Eugen Löbig"]
        assert parts(entity.structure) == {
            "nicknames": ["Muckel"],
            "unsplit": ["Eugen Löbig"],
        }

    def test_each_run_between_quotes_after_the_surname_is_a_suffix(self):
        entity, _ = record_of(
            person("I1", "Eugen /Löbig/ Jr. 'Muckel' Sr."), "i1"
        )
        assert parts(entity.structure)["suffixes"] == ["Jr.", "Sr."]
        assert parts(entity.structure)["nicknames"] == ["Muckel"]

    def test_a_given_name_parts_quote_with_a_comma_is_read_once(self):
        ida, _ = record_of(
            person("I7", "Ida /Sörne/", "2 GIVN 'Ida, Peppi'"), "i7"
        )
        assert parts(ida.structure) == {
            "given": ["Ida"],
            "surnames": [("Sörne", "other")],
            "nicknames": ["Ida, Peppi"],
        }

    def test_a_placeholder_inside_a_part_is_dropped_and_counted(self):
        # decision C, as in the NAME value
        text = person(
            "I1", "", "2 GIVN Eugen ?", "2 SURN ? Löbig", "2 NICK Cilli ?"
        )
        entity, outcomes = record_of(text, "i1")
        assert (entity.label, entity.aliases) == ("Eugen Löbig", ["Cilli"])
        assert parts(entity.structure) == {
            "given": ["Eugen"],
            "surnames": [("Löbig", "other")],
            "nicknames": ["Cilli"],
        }
        assert outcomes["NAME part without a word"] == 3

    @pytest.mark.parametrize(
        ("name", "lines", "expected"),
        [
            ("Eugen ? /Löbig/", [], {"given": ["Eugen"],
                                    "surnames": [("Löbig", "other")]}),
            ("? /Löbig/", ["2 TYPE aka"], {"surnames": [("Löbig", "other")]}),
            ("Eugen /Löbig/ ?", [], {"given": ["Eugen"],
                                    "surnames": [("Löbig", "other")]}),
            ("Eugen /___/", [], {"given": ["Eugen"]}),
        ],
    )  # fmt: skip
    def test_each_field_leaves_its_placeholder_out(
        self, name, lines, expected
    ):
        entity, outcomes = record_of(person("I1", name, *lines), "i1")
        assert parts(entity.structure) == expected
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize(
        ("name", "expected"),
        [("'Cilli'", {"nicknames": ["Cilli"]}),
         ("/Löbig/", {"surnames": [("Löbig", "other")]})],
    )  # fmt: skip
    def test_a_record_of_one_field_stands(self, name, expected):
        entity, _ = record_of(person("I2", name), "i2")
        assert parts(entity.structure) == expected

    def test_a_full_form_keeps_no_placeholder(self):
        entity, _ = record_of(person("I1", "Eugen /?/", "2 NPFX Dr."), "i1")
        assert entity.aliases == ["Dr. Eugen"]

    def test_a_full_form_does_not_repeat_the_values_first_word(self):
        entity, _ = record_of(
            person("I1", "Eugen /Löbig/", "2 NPFX Dr. Eugen"), "i1"
        )
        assert entity.aliases == ["Dr. Eugen Löbig"]

    def test_brackets_give_the_label_from_any_name(self):
        # the bracket tier reads every NAME, not the first alone
        result = adapt_gedcom(person("I7", "?", "1 NAME (Peppi)"), EXPORT)
        (ida,) = result.batch.entities
        assert ida.label == "(Peppi)"
        assert not result.warnings

    def test_a_word_less_bracket_beside_one_with_a_word(self):
        _, outcomes = record_of(person("I7", "(Peppi) (?)"), "i7")
        assert outcomes["NAME bracketed part"] == 1
        assert outcomes["NAME part without a word"] == 1

    def test_a_married_names_placeholder_surname(self):
        text = person("I17", "Agnes /Löbig/", "1 _MARNM Agnes /?/")
        agnes, outcomes = record_of(text, "i17")
        assert parts(agnes.structure)["unsplit"] == ["Agnes"]
        assert stated(text, "i17", "married_name") == []
        assert outcomes["NAME part without a word"] == 1


class TestRound3Typing:
    """Decision F: a typed name that held a placeholder gives no typed
    claim, counted, so its remnant never reads as the whole typed name."""

    def test_a_typed_name_with_a_placeholder_gives_no_typed_claim(self):
        text = person("I1", "Eugen /?/", "2 TYPE birth")
        entity, outcomes = record_of(text, "i1")
        assert entity.label == "Eugen"
        assert stated(text, "i1", "birth_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1

    def test_a_typed_part_with_a_placeholder_gives_no_typed_claim(self):
        text = person("I1", "Eugen /Löbig/", "2 SURN ? Löbig", "2 TYPE birth")
        assert stated(text, "i1", "birth_name") == []

    def test_a_married_name_with_a_placeholder_gives_no_typed_claim(self):
        text = person("I17", "Agnes /Tarle/", "1 _MARNM Agnes ? /Löbig/")
        agnes, outcomes = record_of(text, "i17")
        assert stated(text, "i17", "married_name") == []
        assert ("Löbig", "married") in parts(agnes.structure)["surnames"]
        assert outcomes["typed NAME with a placeholder"] == 1

    def test_a_typed_name_without_one_keeps_its_claim(self):
        text = person("I1", "Eugen /Löbig/", "2 TYPE birth")
        assert stated(text, "i1", "birth_name") == [("Eugen Löbig", [])]


class TestRound3Families:
    def test_one_person_as_husband_and_wife_gives_no_parents(self):
        # decision E: one person as both father and mother is no reading
        # the record can mean, so neither claim is made
        text = (
            family("1 MARR Y", "1 CHIL @I12@")
            .replace("1 WIFE @I17@", "1 WIFE @I11@")
            .replace(
                "0 @F5@ FAM", "0 @I12@ INDI\n1 NAME Rudolf /Löbig/\n0 @F5@ FAM"
            )
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert unions(result.batch) == {}
        assert on(result.batch, "i12", "father") == []
        assert on(result.batch, "i12", "mother") == []
        assert result.outcomes["FAM one person as HUSB and WIFE"] == 1

    def test_a_parent_listed_as_their_own_child_loses_that_link_alone(self):
        # decision CR (#461): a family listing its husband as its child
        # makes no claim that he is his own father, and counts it; the
        # other parent's claim stands and the document is adapted
        result = adapt_gedcom(family("1 CHIL @I11@"), artifact=EXPORT)
        assert on(result.batch, "i11", "father") == []
        assert on(result.batch, "i11", "mother") == [("mother", "i17", [])]
        assert result.outcomes["FAM one person as parent and CHIL"] == 1


def test_a_particle_and_surname_restated_apart_give_their_form():
    # the value holds 'de' and 'Sörne' but not 'de Sörne', which the parts
    # make one surname: that form is a name form of the person's
    text = person("I7", "de Ida /Sörne/", "2 SPFX de", "2 SURN Sörne")
    ida, outcomes = record_of(text, "i7")
    assert ("de Sörne", "other") in parts(ida.structure)["surnames"]
    assert "de Sörne" in ida.aliases
    assert outcomes["NAME parts not restated"] == 1


class TestGivnBracketsAndQuotes:
    """A GIVN's brackets and quotes read as the value's do (ADR-0035 §6):
    a quote marks a nickname, and a bracket, which may hold a maiden name,
    a later name or a note, is an alias and an unsplit form. Either keeps
    a person whose only word it holds."""

    def test_a_givn_bracket_alone_keeps_its_person_as_an_unsplit_form(self):
        ida, outcomes = record_of(person("I7", "", "2 GIVN (Peppi)"), "i7")
        assert (ida.label, ida.aliases) == ("(Peppi)", [])
        assert parts(ida.structure) == {"unsplit": ["Peppi"]}
        assert outcomes["NAME bracketed part"] == 1

    def test_a_givn_quote_alone_keeps_its_person_as_a_nickname(self):
        text = person("I7", "", "2 GIVN 'Peppi'")
        ida, outcomes = record_of(text, "i7")
        assert (ida.label, ida.aliases) == ("'Peppi'", [])
        assert parts(ida.structure) == {"nicknames": ["Peppi"]}
        assert stated(text, "i7", "nickname") == [("Peppi", [])]
        assert "NAME bracketed part" not in outcomes

    def test_an_aka_name_with_a_quote_gives_both_nickname_claims(self):
        # the aka types the whole name as a nickname, and the quote marks
        # one within it: each is a claim
        text = person("I1", "Eugen 'Muckel' /Löbig/", "2 TYPE aka")
        assert stated(text, "i1", "nickname") == [
            ("Eugen 'Muckel' Löbig", []),
            ("Muckel", []),
        ]


class TestRound4:
    """Re-review 3: a GIVN's quote gives a nickname claim; a quote around a
    bracket is no placeholder; a placeholder anywhere in a typed NAME
    blocks its claim (decision K); fallback labels keep their marks
    (decision L); a connector between words stays (decision M)."""

    @pytest.mark.parametrize("quoted", ["'Peppi'", '"Peppi"', "‘Peppi’",
                                        "“Peppi”"])  # fmt: skip
    def test_a_givn_quote_gives_a_nickname_claim(self, quoted):
        text = person("I7", "Ida /Sörne/", f"2 GIVN Ida {quoted}")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["nicknames"] == ["Peppi"]
        assert stated(text, "i7", "nickname") == [("Peppi", [])]
        (claim,) = claims_for(
            adapt_gedcom(text, artifact=EXPORT).batch, "i7", "nickname"
        )
        assert claim.source.transcription_excerpt.text.startswith(
            "1 NAME Ida /Sörne/"
        )

    @pytest.mark.parametrize("held", ["(?)", "'?'"])
    def test_a_givn_placeholder_gives_no_claim(self, held):
        text = person("I7", "Ida /Sörne/", f"2 GIVN Ida {held}")
        ida, outcomes = record_of(text, "i7")
        assert stated(text, "i7", "nickname") == []
        assert outcomes["NAME part without a word"] == 1
        assert parts(ida.structure)["given"] == ["Ida"]

    @pytest.mark.parametrize("quote", ["'(Peppi)'", "“(Peppi)”"])
    def test_a_quote_holding_a_bracket_is_no_placeholder(self, quote):
        text = person("I7", f"Ida {quote} /Sörne/", "2 TYPE birth")
        ida, outcomes = record_of(text, "i7")
        assert stated(text, "i7", "birth_name") == [("Ida Sörne", [])]
        assert "typed NAME with a placeholder" not in outcomes
        assert "NAME part without a word" not in outcomes
        assert outcomes["NAME bracketed part"] == 1
        assert ida.aliases == ["Peppi"]

    def test_a_givn_quote_holding_a_bracket_is_no_placeholder(self):
        text = person("I7", "Ida /Sörne/", "2 GIVN Ida '(Peppi)'")
        _, outcomes = record_of(text, "i7")
        assert "NAME part without a word" not in outcomes
        assert outcomes["NAME bracketed part"] == 1

    @pytest.mark.parametrize(
        ("name", "lines", "prop"),
        [
            ("Eugen (?) /Löbig/", ["2 TYPE birth"], "birth_name"),
            ("Eugen /Löbig/ (?)", ["2 TYPE birth"], "birth_name"),
            ("Eugen /(?)/", ["2 TYPE birth"], "birth_name"),
            ("Eugen '?' /Löbig/", ["2 TYPE birth"], "birth_name"),
            ("Agnes /Löbig/", ["2 TYPE married", "2 GIVN Agnes (?)"],
             "married_name"),
            ("Agnes /Löbig/", ["2 TYPE married", "2 SPFX ?"], "married_name"),
        ],
    )  # fmt: skip
    def test_a_placeholder_anywhere_blocks_the_typed_claim(
        self, name, lines, prop
    ):
        # decision K: a tree writes "(?)" when unsure of the name
        text = person("I1", name, *lines)
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", prop) == []
        assert outcomes["typed NAME with a placeholder"] == 1

    def test_a_nicks_placeholder_does_not_block_the_typed_claim(self):
        text = person("I1", "Eugen /Löbig/", "2 TYPE birth", "2 NICK ?")
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == [("Eugen Löbig", [])]
        assert "typed NAME with a placeholder" not in outcomes
        assert outcomes["NAME part without a word"] == 1

    def test_an_untyped_name_with_a_placeholder_is_no_typed_count(self):
        _, outcomes = record_of(person("I1", "Eugen ? /Löbig/"), "i1")
        assert "typed NAME with a placeholder" not in outcomes

    @pytest.mark.parametrize(
        ("lines", "label"),
        [
            (["1 NAME ?", "2 NICK Muckel", "1 _MARNM Agnes /Löbig/"],
             "Muckel"),
            (["1 NAME (Peppi)", "2 GIVN (Cilli)"], "(Peppi)"),
            (["1 NAME ?", "1 NAME", "2 GIVN (Peppi)"], "(Peppi)"),
            (["1 NAME ?", "1 NAME ?", "2 NICK Muckel"], "Muckel"),
            (["1 NAME ?", "1 _MARNM /?/", "1 _MARNM Agnes /Löbig/"],
             "Agnes Löbig"),
            (["1 NAME ?", "2 NICK 'Muckel'"], "'Muckel'"),
            (["1 NAME ?", "1 _MARNM (Peppi)"], "(Peppi)"),
        ],
    )  # fmt: skip
    def test_the_tiers_read_every_name_in_order(self, lines, label):
        text = "\n".join(["0 @I7@ INDI", *lines, "0 TRLR", ""])
        result = adapt_gedcom(text, artifact=EXPORT)
        (entity,) = result.batch.entities
        assert entity.label == label
        assert not result.warnings

    def test_a_stray_closing_bracket_is_counted_once(self):
        entity, outcomes = record_of(person("I1", "Eugen ) /Löbig/"), "i1")
        assert entity.label == "Eugen Löbig"
        assert outcomes["NAME part without a word"] == 1

    def test_an_unclosed_bracket_stuck_to_a_word_leaves_the_label(self):
        entity, outcomes = record_of(
            person("I7", "Ida (Pep(pi) /Sörne/"), "i7"
        )
        assert "(" not in entity.label
        assert outcomes["NAME part without a word"] == 1

    def test_two_brackets_are_two_unsplit_forms(self):
        entity, outcomes = record_of(
            person("I7", "Ida (Peppi) (Cilli) /Sörne/"), "i7"
        )
        assert entity.label == "Ida Sörne"
        assert entity.aliases == ["Peppi", "Cilli"]
        assert outcomes["NAME bracketed part"] == 2

    @pytest.mark.parametrize(
        ("name", "lines"),
        [("Ida () /Sörne/", []), ("Eugen ' ' /Löbig/", []),
         ("Ida /Sörne/", ["2 GIVN Ida ()"]),
         ("Ida /Sörne/", ["2 GIVN Ida (Peppi ?)"])],
    )  # fmt: skip
    def test_each_empty_mark_or_placeholder_is_counted_once(self, name, lines):
        _, outcomes = record_of(person("I7", name, *lines), "i7")
        assert outcomes["NAME part without a word"] == 1

    def test_a_family_of_a_child_alone_is_no_one_person_couple(self):
        text = family().replace(
            "1 HUSB @I11@\n1 WIFE @I17@\n", "1 CHIL @I11@\n"
        )
        result = adapt_gedcom(text, artifact=EXPORT)
        assert "FAM one person as HUSB and WIFE" not in result.outcomes

    def test_a_prefix_beside_no_given_name_is_no_restated_count(self):
        _, outcomes = record_of(person("I1", "/Löbig/", "2 NPFX Dr."), "i1")
        assert "NPFX restated as the whole given part" not in outcomes

    def test_a_particle_goes_on_each_surname_piece(self):
        # a two-piece SURN beside an SPFX: each piece takes the particle,
        # and the export adapts
        text = person(
            "I1", "Eugen /Löbig/", "2 SURN Löbig, Sörne", "2 SPFX von"
        )
        entity, _ = record_of(text, "i1")
        surnames = [w for w, _ in parts(entity.structure)["surnames"]]
        assert {"von Löbig", "von Sörne"} <= set(surnames)
        assert structure_problems(entity) == []

    @pytest.mark.parametrize(
        ("name", "label", "surnames"),
        [("Ida /Löbig - Sörne/", "Ida Löbig - Sörne",
          [("Löbig - Sörne", "other")]),
         ("Eugen & Ida /Löbig/", "Eugen & Ida Löbig", [("Löbig", "other")])],
    )  # fmt: skip
    def test_a_connector_between_words_is_kept(self, name, label, surnames):
        # decision M
        entity, outcomes = record_of(person("I7", name), "i7")
        assert entity.label == label
        assert parts(entity.structure)["surnames"] == surnames
        assert "NAME part without a word" not in outcomes

    @pytest.mark.parametrize("name", ["-", "&", "- /Löbig/"])
    def test_a_connector_alone_is_a_placeholder(self, name):
        text = person("I7", name)
        result = adapt_gedcom(text, artifact=EXPORT)
        labels = [e.label for e in result.batch.entities]
        assert all("-" not in label and "&" not in label for label in labels)

    def test_a_prefix_that_holds_the_whole_name_is_its_form(self):
        entity, _ = record_of(person("I1", "Dr.", "2 NPFX Dr. med."), "i1")
        assert entity.aliases == ["Dr. med."]
        entity, _ = record_of(
            person("I1", "Eugen /Löbig/", "2 NPFX Dr. Eugen Löbig"), "i1"
        )
        assert entity.aliases == ["Dr. Eugen Löbig"]

    def test_digits_are_words(self):
        entity, _ = record_of(person("I1", "1890"), "i1")
        assert entity.label == "1890"

    @pytest.mark.parametrize(
        ("wives", "union"),
        [(["1 WIFE @I11@", "1 WIFE @I17@"], {}),
         (["1 WIFE @I17@", "1 WIFE @I11@"], {"f5": "partnership"})],
    )  # fmt: skip
    def test_one_person_as_husband_and_first_wife_is_read_literally(
        self, wives, union
    ):
        # deliberate: decision E reads the first two partner lines (#353)
        text = family().replace("1 WIFE @I17@", "\n".join(wives))
        assert unions(adapt_gedcom(text, artifact=EXPORT).batch) == union


class TestRound5:
    """Re-review 4: a quote around a bracket opens and closes a word; a
    connector joins words of one field only, so one at a slash, a bracket
    or a quote is a placeholder (decisions M, Y); '+' and runs join
    nothing (decision W); SPFX pieces pair with SURN pieces (decision X).
    """

    TWO_NICKNAMES = {
        "given": ["Eugen"],
        "surnames": [("Löbig", "other")],
        "nicknames": ["Muckel", "Cilli"],
        "unsplit": ["Peppi"],
    }

    @pytest.mark.parametrize(
        "name",
        [
            "Eugen 'Muckel' (Peppi) 'Cilli' /Löbig/",
            'Eugen "Muckel" (Peppi) "Cilli" /Löbig/',
            "Eugen ‘Muckel’ (Peppi) ‘Cilli’ /Löbig/",
        ],
    )
    def test_a_bracket_between_two_quotes_joins_neither(self, name):
        text = person("I1", name)
        eugen, outcomes = record_of(text, "i1")
        assert parts(eugen.structure) == self.TWO_NICKNAMES
        assert eugen.label == name.replace(" (Peppi)", "").replace("/", "")
        assert eugen.aliases == ["Peppi"]
        assert [value for value, _ in stated(text, "i1", "nickname")] == [
            "Muckel",
            "Cilli",
        ]
        assert outcomes["NAME bracketed part"] == 1

    def test_a_bracket_between_two_givn_quotes_joins_neither(self):
        text = person(
            "I1", "Eugen /Löbig/", "2 GIVN Eugen 'Muckel' (Peppi) 'Cilli'"
        )
        eugen, _ = record_of(text, "i1")
        assert parts(eugen.structure) == self.TWO_NICKNAMES
        assert eugen.aliases == ["Muckel", "Cilli", "Peppi"]
        assert [value for value, _ in stated(text, "i1", "nickname")] == [
            "Muckel",
            "Cilli",
        ]

    def test_a_bracket_between_two_quotes_after_the_surname(self):
        text = person("I1", "Eugen /Löbig/ 'Muckel' (Peppi) 'Cilli'")
        eugen, _ = record_of(text, "i1")
        assert parts(eugen.structure) == self.TWO_NICKNAMES
        assert eugen.label == "Eugen Löbig 'Muckel' 'Cilli'"

    def test_a_bracket_between_two_quotes_of_a_typed_name(self):
        text = person(
            "I7", "Ida 'Muckel' (Peppi) 'Cilli' /Sörne/", "2 TYPE birth"
        )
        assert stated(text, "i7", "birth_name") == [
            ("Ida 'Muckel' 'Cilli' Sörne", [])
        ]
        assert [value for value, _ in stated(text, "i7", "nickname")] == [
            "Muckel",
            "Cilli",
        ]

    @pytest.mark.parametrize(
        "name",
        [
            "Ida ‘(Peppi)’ /Sörne/",
            "Ida ' (Peppi) ' /Sörne/",
            "Ida '(Peppi)'/Sörne/",
            "Ida „(Peppi)“ /Sörne/",
        ],
    )
    def test_each_quote_form_around_a_bracket_is_no_placeholder(self, name):
        text = person("I7", name, "2 TYPE birth")
        ida, outcomes = record_of(text, "i7")
        assert stated(text, "i7", "birth_name") == [("Ida Sörne", [])]
        assert "NAME part without a word" not in outcomes
        assert ida.aliases == ["Peppi"]

    def test_an_empty_bracket_in_a_quote_is_counted_once(self):
        _, outcomes = record_of(person("I7", "Ida '()' /Sörne/"), "i7")
        assert outcomes["NAME part without a word"] == 1

    def test_a_quoted_bracket_without_slashes_leaves_no_mark(self):
        ida, _ = record_of(person("I7", "Ida '(Peppi)' Sörne"), "i7")
        assert ida.label == "Ida Sörne"
        assert parts(ida.structure)["unsplit"] == ["Peppi", "Ida Sörne"]

    def test_two_givn_quotes_give_two_claims(self):
        text = person("I7", "Ida /Sörne/", "2 GIVN Ida 'Peppi' 'Cilli'")
        assert [value for value, _ in stated(text, "i7", "nickname")] == [
            "Peppi",
            "Cilli",
        ]

    @pytest.mark.parametrize(
        ("name", "lines", "label", "prop"),
        [
            ("Ida /-/ Wwe.", ["2 TYPE married"], "Ida Wwe.", "married_name"),
            ("Leopold /-/ Jr.", ["2 TYPE birth"], "Leopold Jr.",
             "birth_name"),
            ("Agnes - /Löbig/", ["2 TYPE married"], "Agnes Löbig",
             "married_name"),
            ("Agnes /- Löbig/", ["2 TYPE married"], "Agnes Löbig",
             "married_name"),
            ("Agnes & /Löbig/", ["2 TYPE married"], "Agnes Löbig",
             "married_name"),
            ("Agnes /Löbig -/", ["2 TYPE married"], "Agnes Löbig",
             "married_name"),
            ("Ida 'Peppi' - /Sörne/", ["2 TYPE birth"], "Ida 'Peppi' Sörne",
             "birth_name"),
            ("Ida - (Peppi) /Sörne/", ["2 TYPE birth"], "Ida Sörne",
             "birth_name"),
            ("Eugen + Ida /Löbig/", ["2 TYPE birth"], "Eugen Ida Löbig",
             "birth_name"),
            ("Eugen /Löbig/", ["2 TYPE birth", "2 GIVN Eugen -"],
             "Eugen Löbig", "birth_name"),
        ],
    )  # fmt: skip
    def test_a_connector_at_a_fields_edge_is_a_placeholder(
        self, name, lines, label, prop
    ):
        # decisions M and Y: a dash or '&' joins two words of one field;
        # against a slash, a bracket or a quote it stands for nothing
        text = person("I1", name, *lines)
        entity, outcomes = record_of(text, "i1")
        assert entity.label == label
        assert stated(text, "i1", prop) == []
        assert outcomes["typed NAME with a placeholder"] == 1
        assert outcomes["NAME part without a word"] == 1

    def test_a_married_name_with_an_edge_dash_is_blocked(self):
        text = person("I1", "Agnes /Löbig/", "1 _MARNM Agnes - /Löbig/")
        agnes, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "married_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1
        assert outcomes["NAME part without a word"] == 1
        assert parts(agnes.structure)["surnames"] == [
            ("Löbig", "other"),
            ("Löbig", "married"),
        ]

    def test_a_married_name_with_a_dash_beside_its_bracket_is_blocked(self):
        text = person(
            "I1", "Agnes /Löbig/", "1 _MARNM Agnes - (Peppi) /Löbig/"
        )
        agnes, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "married_name") == []
        assert agnes.aliases == []
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize(
        ("name", "label"),
        [
            ("? - Ida /Löbig/", "Ida Löbig"),
            ("Eugen - ? /Löbig/", "Eugen Löbig"),
            ("Ida /Löbig -- Sörne/", "Ida Löbig Sörne"),
            ("Ida /Löbig -& Sörne/", "Ida Löbig Sörne"),
            ("Löbig - /Sörne/", "Löbig Sörne"),
        ],
    )
    def test_a_connector_beside_no_word_or_a_run_is_dropped(self, name, label):
        entity, outcomes = record_of(person("I1", name), "i1")
        assert entity.label == label
        assert outcomes["NAME part without a word"] == (
            2 if "?" in name else 1
        )

    @pytest.mark.parametrize(
        ("name", "label", "surname"),
        [
            ("Ida /Löbig - Sörne/", "Ida Löbig - Sörne", "Löbig - Sörne"),
            ("Ida /Löbig – Sörne/", "Ida Löbig – Sörne", "Löbig – Sörne"),
            ("Ida /Löbig & Sörne/", "Ida Löbig & Sörne", "Löbig & Sörne"),
        ],
    )
    def test_a_single_dash_or_and_within_a_surname_stays(
        self, name, label, surname
    ):
        text = person("I7", name, "2 TYPE birth")
        ida, outcomes = record_of(text, "i7")
        assert ida.label == label
        assert parts(ida.structure)["surnames"] == [(surname, "birth")]
        assert stated(text, "i7", "birth_name") == [(label, [])]
        assert "NAME part without a word" not in outcomes

    def test_a_connector_within_a_quote_or_a_bracket_stays(self):
        text = person("I1", "Eugen 'Muckel - Cilli' (Peppi & Cilli) /Löbig/")
        eugen, outcomes = record_of(text, "i1")
        assert eugen.label == "Eugen 'Muckel - Cilli' Löbig"
        assert eugen.aliases == ["Peppi & Cilli"]
        assert parts(eugen.structure)["nicknames"] == ["Muckel - Cilli"]
        assert "NAME part without a word" not in outcomes

    @pytest.mark.parametrize("lines", [[], ["2 TYPE aka"]])
    def test_a_givn_connector_is_no_given_name(self, lines):
        text = person("I1", "Eugen /Löbig/", *lines, "2 GIVN Eugen & Ida")
        eugen, outcomes = record_of(text, "i1")
        field = "nicknames" if lines else "given"
        assert parts(eugen.structure)[field] == ["Eugen", "Ida"]
        assert "NAME part without a word" not in outcomes

    @pytest.mark.parametrize(
        ("name", "lines"),
        [
            ("Eugen) /Löbig/", []),
            ("Eugen /Löbig/", ["2 SURN Löbig)"]),
            ("Eugen (Peppi ()) /Löbig/", []),
            ("Eugen /Löbig/", ["2 GIVN Eugen (Peppi ())"]),
        ],
    )
    def test_a_stray_or_a_nested_empty_bracket_blocks(self, name, lines):
        text = person("I1", name, "2 TYPE birth", *lines)
        eugen, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1
        assert outcomes["NAME part without a word"] >= 1
        assert ")" not in eugen.label

    def test_a_glued_stray_bracket_parts_its_words(self):
        ida, outcomes = record_of(person("I7", "Ida(Pep /Sörne/"), "i7")
        assert ida.label == "Ida Pep Sörne"
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize(
        ("spfx", "surn", "surnames", "counted"),
        [
            ("von, de", "Löbig, Sörne", ["von Löbig", "de Sörne"], False),
            ("von", "Löbig", ["von Löbig"], False),
            ("von", "Löbig, Sörne", ["von Löbig", "von Sörne"], True),
            ("von, de", "Löbig", ["von de Löbig"], True),
        ],
    )
    def test_spfx_pieces_pair_with_surn_pieces(
        self, spfx, surn, surnames, counted
    ):
        # decision X
        text = person("I1", "", "2 GIVN Eugen", f"2 SPFX {spfx}",
                      f"2 SURN {surn}")  # fmt: skip
        eugen, outcomes = record_of(text, "i1")
        assert [s for s, _ in parts(eugen.structure)["surnames"]] == surnames
        assert eugen.label == " ".join(["Eugen", *surnames])
        assert (
            "SPFX particles not matching SURN pieces" in outcomes
        ) is counted

    def test_a_particle_without_a_surname_stays_in_the_label(self):
        text = person("I1", "", "2 GIVN Eugen", "2 SPFX von")
        eugen, outcomes = record_of(text, "i1")
        assert eugen.label == "Eugen von"
        assert outcomes["SPFX without a surname"] == 1

    @pytest.mark.parametrize(
        ("quoted", "nick"),
        [("„Peppi“", "‚Cilli‘"), ("»Peppi«", "«Cilli»")],
    )
    def test_german_quotes_and_guillemets_mark_nicknames(self, quoted, nick):
        text = person("I7", f"Ida {quoted} /Sörne/", f"2 NICK {nick}")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["given"] == ["Ida"]
        assert parts(ida.structure)["nicknames"] == ["Peppi", "Cilli"]
        assert ida.label == f"Ida {quoted} Sörne"

    def test_a_prefix_holding_the_whole_name_is_not_written_twice(self):
        text = person("I1", "Eugen /Löbig/", "2 NPFX Dr. Eugen Löbig Jr.")
        eugen, _ = record_of(text, "i1")
        assert eugen.aliases == ["Dr. Eugen Löbig Jr."]

    def test_a_nick_holding_a_quoted_bracket_keeps_the_bracket(self):
        text = person("I1", "Eugen /Löbig/", "2 NICK Muckel '(Peppi)'")
        eugen, _ = record_of(text, "i1")
        assert parts(eugen.structure)["nicknames"] == ["Muckel (Peppi)"]

    def test_a_quoted_bracket_glued_after_the_surname_is_read(self):
        text = person("I7", "Ida /Sörne/'(Peppi)'", "2 TYPE birth")
        ida, outcomes = record_of(text, "i7")
        assert stated(text, "i7", "birth_name") == [("Ida Sörne", [])]
        assert ida.aliases == ["Peppi"]
        assert "NAME part without a word" not in outcomes

    def test_a_connector_beside_an_emptied_bracket_is_a_placeholder(self):
        eugen, outcomes = record_of(
            person("I1", "Eugen - (?) Ida /Löbig/"), "i1"
        )
        assert eugen.label == "Eugen Ida Löbig"
        assert outcomes["NAME part without a word"] == 2

    def test_a_nested_empty_bracket_in_a_part_blocks(self):
        text = person(
            "I1", "Eugen /Löbig/ Jr.", "2 TYPE birth", "2 NSFX Jr. (Sr. ())"
        )
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1

    def test_a_nested_bracket_with_a_word_is_no_placeholder(self):
        text = person("I1", "Eugen (Pep(pi)) /Löbig/", "2 TYPE birth")
        eugen, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == [("Eugen Löbig", [])]
        assert eugen.aliases == ["Peppi"]
        assert "NAME part without a word" not in outcomes

    def test_a_married_surname_leaves_its_bracket_marks(self):
        text = person("I1", "Agnes /Löbig/", "1 _MARNM Agnes /Löbig (Peppi)/")
        agnes, _ = record_of(text, "i1")
        assert ("Löbig Peppi", "married") in parts(agnes.structure)["surnames"]

    def test_a_givn_connector_beside_its_bracket_is_a_placeholder(self):
        text = person(
            "I1", "Eugen /Löbig/", "2 TYPE birth", "2 GIVN Eugen - (Peppi)"
        )
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == []
        assert outcomes["NAME part without a word"] == 1

    def test_a_slashless_connector_beside_a_quote_is_a_placeholder(self):
        eugen, outcomes = record_of(
            person("I1", "Eugen - 'Muckel' Löbig"), "i1"
        )
        assert parts(eugen.structure)["unsplit"] == ["Eugen Löbig"]
        assert outcomes["NAME part without a word"] == 1

    def test_a_connector_beside_an_emptied_bracket_in_a_nick(self):
        text = person("I1", "Eugen /Löbig/", "2 NICK Muckel - (?) Cilli")
        eugen, outcomes = record_of(text, "i1")
        assert parts(eugen.structure)["nicknames"] == ["Muckel Cilli"]
        assert outcomes["NAME part without a word"] == 2

    def test_a_givn_connector_before_its_bracket_joins_nothing(self):
        text = person("I1", "Eugen Ida /Löbig/", "2 GIVN Eugen - (Peppi) Ida")
        eugen, outcomes = record_of(text, "i1")
        assert parts(eugen.structure)["given"] == ["Eugen", "Ida"]
        assert outcomes["NAME part without a word"] == 1


class TestRound6:
    """Re-review 5's cheap items: a quote against a slash, SPFX and SURN
    paired by slot, a quoted bracket before a GIVN's comma, a NICK in
    mixed quotes, a nested placeholder counted once, and pins."""

    def test_a_quote_against_a_slash_is_a_quote(self):
        text = person("I1", "Eugen - 'Muckel'/Löbig/", "2 TYPE birth")
        eugen, outcomes = record_of(text, "i1")
        assert eugen.label == "Eugen 'Muckel' Löbig"
        assert parts(eugen.structure)["nicknames"] == ["Muckel"]
        assert stated(text, "i1", "birth_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1
        assert outcomes["NAME part without a word"] == 1

    def test_a_quoted_placeholder_against_a_slash_leaves_no_mark(self):
        ida, outcomes = record_of(person("I7", "Ida '? Muckel'/Sörne/"), "i7")
        assert ida.label == "Ida 'Muckel' Sörne"
        assert parts(ida.structure)["nicknames"] == ["Muckel"]
        assert outcomes["NAME part without a word"] == 1

    @pytest.mark.parametrize(
        ("spfx", "surnames", "label"),
        [
            ("von,", ["von Löbig", "Sörne"], "Eugen von Löbig Sörne"),
            ("?, de", ["Löbig", "de Sörne"], "Eugen Löbig de Sörne"),
            (", de", ["Löbig", "de Sörne"], "Eugen Löbig de Sörne"),
        ],
    )
    def test_spfx_and_surn_pair_slot_by_slot(self, spfx, surnames, label):
        text = person("I1", "", "2 GIVN Eugen", f"2 SPFX {spfx}",
                      "2 SURN Löbig, Sörne")  # fmt: skip
        eugen, outcomes = record_of(text, "i1")
        assert [s for s, _ in parts(eugen.structure)["surnames"]] == surnames
        assert eugen.label == label
        assert "SPFX particles not matching SURN pieces" not in outcomes

    def test_an_empty_surn_slot_takes_no_particle(self):
        text = person("I1", "", "2 GIVN Eugen", "2 SPFX von, de",
                      "2 SURN , Sörne")  # fmt: skip
        eugen, _ = record_of(text, "i1")
        assert [s for s, _ in parts(eugen.structure)["surnames"]] == [
            "de Sörne"
        ]

    @pytest.mark.parametrize(
        ("lines", "counted"),
        [
            (["2 SURN Löbig"], False),
            (["2 SPFX von"], False),
            (["2 SPFX von, de", "2 SURN Löbig"], True),
        ],
    )
    def test_the_mismatch_count_needs_both_parts(self, lines, counted):
        text = person("I1", "", "2 GIVN Eugen", *lines)
        _, outcomes = record_of(text, "i1")
        assert (
            "SPFX particles not matching SURN pieces" in outcomes
        ) is counted

    def test_a_givn_quoted_bracket_before_a_comma_is_read(self):
        text = person("I1", "Eugen /Löbig/", "2 TYPE birth",
                      "2 GIVN Ida '(Peppi)', Agnes")  # fmt: skip
        eugen, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == [("Eugen Löbig", [])]
        assert "NAME part without a word" not in outcomes
        assert parts(eugen.structure)["unsplit"] == ["Peppi"]

    @pytest.mark.parametrize(
        "nick", ["„Peppi”", "„Peppi“", "‚Peppi‘", "»Peppi«", "«Peppi»"]
    )
    def test_a_nick_in_any_quotes_loses_them(self, nick):
        text = person("I7", "Ida /Sörne/", f"2 NICK {nick}")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["nicknames"] == ["Peppi"]
        assert ida.aliases == ["Peppi"]

    @pytest.mark.parametrize(
        ("name", "count"),
        [
            ("Ida (Peppi (?)) /Sörne/", 1),
            ("Ida (Peppi ()) /Sörne/", 1),
            ("Ida (Peppi (())) /Sörne/", 1),
            ("Ida (Peppi () ()) /Sörne/", 2),
        ],
    )
    def test_a_nested_placeholder_is_counted_once(self, name, count):
        ida, outcomes = record_of(person("I7", name), "i7")
        assert outcomes["NAME part without a word"] == count
        assert ida.aliases == ["Peppi"]

    @pytest.mark.parametrize("quoted", ["„Peppi“", "‚Peppi‘", "»Peppi«",
                                        "«Peppi»"])  # fmt: skip
    def test_each_german_quote_marks_a_nickname(self, quoted):
        text = person("I7", f"Ida {quoted} /Sörne/")
        ida, _ = record_of(text, "i7")
        assert parts(ida.structure)["nicknames"] == ["Peppi"]
        assert stated(text, "i7", "nickname") == [("Peppi", [])]

    def test_a_quoted_bracket_at_the_start_is_read(self):
        text = person("I7", "'(Peppi)' Ida /Sörne/", "2 TYPE birth")
        _, outcomes = record_of(text, "i7")
        assert stated(text, "i7", "birth_name") == [("Ida Sörne", [])]
        assert "NAME part without a word" not in outcomes

    def test_a_married_name_with_a_dash_after_its_bracket_is_blocked(self):
        text = person(
            "I1", "Agnes /Löbig/", "1 _MARNM Agnes (Peppi) - Cilli /Löbig/"
        )
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "married_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1
        assert outcomes["NAME part without a word"] == 1

    def test_a_givn_dash_after_its_quote_is_blocked(self):
        text = person("I1", "Agnes Cilli /Löbig/", "2 TYPE birth",
                      "2 GIVN Agnes 'Peppi' - Cilli")  # fmt: skip
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == []
        assert outcomes["typed NAME with a placeholder"] == 1

    def test_a_quote_glued_after_the_surname_is_a_quote(self):
        text = person("I1", "Eugen /Löbig/'Muckel'")
        eugen, _ = record_of(text, "i1")
        assert parts(eugen.structure)["nicknames"] == ["Muckel"]
        assert stated(text, "i1", "nickname") == [("Muckel", [])]

    def test_a_givn_quoted_bracket_after_a_comma_is_read(self):
        text = person("I1", "Agnes /Löbig/", "2 TYPE birth",
                      "2 GIVN Agnes,'(Peppi)'")  # fmt: skip
        _, outcomes = record_of(text, "i1")
        assert stated(text, "i1", "birth_name") == [("Agnes Löbig", [])]
        assert "NAME part without a word" not in outcomes

    @pytest.mark.parametrize("nick", ["„Cilli", "'Cilli", "Cilli“"])
    def test_a_nick_with_one_quote_mark_keeps_it(self, nick):
        text = person("I2", "Cäcilie /Tarle/", f"2 NICK {nick}")
        caecilie, _ = record_of(text, "i2")
        assert parts(caecilie.structure)["nicknames"] == [nick]

    def test_a_suffix_stated_only_across_two_brackets_gives_a_form(self):
        # the brackets are two forms, so neither restates the suffix whole
        text = "\n".join([
            "0 @I1@ INDI",
            "1 NAME (Peppi) (Peppi)", "2 NSFX 'Peppi' Peppi",
            "1 NAME", "2 GIVN Eugen",
            "0 TRLR", "",
        ])  # fmt: skip
        eugen, outcomes = record_of(text, "i1")
        assert eugen.label == "Eugen"
        assert "Peppi Peppi 'Peppi' Peppi" in eugen.aliases
        assert outcomes["NAME parts not restated"] == 1

    def test_a_quoted_placeholder_glued_after_the_surname_leaves_no_mark(self):
        eugen, outcomes = record_of(
            person("I1", "Eugen /Löbig/'? Muckel'"), "i1"
        )
        assert eugen.label == "Eugen Löbig 'Muckel'"
        assert outcomes["NAME part without a word"] == 1


# each form of date the grammar reads, written without a calendar and with
# `{cal}` where the Gregorian calendar may stand before one of its dates
GREGORIAN_FORMS = [
    # exact dates, at each precision
    "{cal}12 MAR 1836",
    "{cal}MAR 1836",
    "{cal}1836",
    "{cal}March 12 1836",
    # approximations
    "ABT {cal}1836",
    "CAL {cal}MAR 1836",
    "EST {cal}12 MAR 1836",
    # ranges
    "BEF {cal}12 MAR 1836",
    "AFT {cal}1836",
    "BET {cal}1836 AND {cal}1840",
    # a period, which a birth reads as a range
    "FROM {cal}1836 TO {cal}1840",
]
# GEDCOM 5.5.1's escape, spaced as the refusal lets it pass, and GEDCOM
# 7's keyword, in either case
GREGORIAN_CALENDARS = [
    "@#DGREGORIAN@ ",
    "@#D GREGORIAN @ ",
    "@# DGREGORIAN@ ",
    "@#dgregorian@ ",
    "GREGORIAN ",
    "gregorian ",
]


def with_gregorian(form: str, calendar: str) -> list[str]:
    """`form` with `calendar` before one or each of its dates, and before
    the whole date."""
    plain = form.format(cal="")
    each = form.format(cal=calendar)
    first = form.replace("{cal}", calendar, 1).format(cal="")
    return list(dict.fromkeys([each, first, calendar + plain]))


class TestTheGregorianCalendar:
    """The Gregorian calendar named before a date (`@#DGREGORIAN@`, or
    GEDCOM 7's `GREGORIAN`) is the one calendar the archive supports
    (ADR-0034), so the date gives exactly the claim it gives written
    without it (#478)."""

    @pytest.mark.parametrize("calendar", GREGORIAN_CALENDARS)
    @pytest.mark.parametrize("form", GREGORIAN_FORMS)
    def test_a_date_reads_as_without_the_calendar(self, form, calendar):
        plain = parse_gedcom_date(form.format(cal=""))
        assert plain[0] is not None
        for date in with_gregorian(form, calendar):
            assert parse_gedcom_date(date) == plain, date

    @pytest.mark.parametrize("calendar", ["@#DGREGORIAN@ ", "GREGORIAN "])
    @pytest.mark.parametrize("form", GREGORIAN_FORMS)
    def test_a_birth_gives_the_same_claim(self, form, calendar):
        def birth(date: str):
            result = adapt_gedcom(
                eugen("1 BIRT", f"2 DATE {date}"), artifact=EXPORT
            )
            return on(result.batch, "i1", "date_of_birth"), result.warnings

        claims, warnings = birth(form.format(cal=""))
        assert claims and not warnings
        for date in with_gregorian(form, calendar):
            assert birth(date) == (claims, warnings), date

    @pytest.mark.parametrize(
        "line",
        [
            "2 DATE @#DGREGORIAN@ 12 MAR 1836",
            # the value starts with a space
            "2 DATE  @#DGREGORIAN@ 12 MAR 1836",
            # the escape's closing @ ends it
            "2 DATE @#DGREGORIAN@12 MAR 1836",
        ],
    )
    def test_eugens_birth_with_the_escape(self, line):
        # a day-precision birth, as without the escape
        result = adapt_gedcom(eugen("1 BIRT", line), artifact=EXPORT)
        assert on(result.batch, "i1", "date_of_birth") == [
            ("date_of_birth", (1836, 3, 12, "day"), [])
        ]
        assert result.warnings == []

    @pytest.mark.parametrize(
        ("date", "plain"),
        [
            # a space left before the calendar within the date
            ("ABT  GREGORIAN 1836", "ABT 1836"),
            ("BET  @#DGREGORIAN@ 1836 AND 1840", "BET 1836 AND 1840"),
            # the glued escape reads as the date after it
            ("@#DGREGORIAN@1836", "1836"),
            ("ABT @#DGREGORIAN@1836", "ABT 1836"),
            # a calendar before the whole date and again before one of its
            # dates, not in a row
            ("GREGORIAN ABT GREGORIAN 1836", "ABT 1836"),
            ("@#DGREGORIAN@ BET @#DGREGORIAN@ 1836 AND 1840",
             "BET 1836 AND 1840"),
        ],
    )  # fmt: skip
    def test_more_spellings_read_as_without_the_calendar(self, date, plain):
        assert parse_gedcom_date(plain)[0] is not None
        assert parse_gedcom_date(date) == parse_gedcom_date(plain)

    @pytest.mark.parametrize("tag", ["RESI", "OCCU"])
    @pytest.mark.parametrize(
        ("date", "plain"),
        [
            ("FROM @#DGREGORIAN@ 1939 TO @#DGREGORIAN@ 1952",
             "FROM 1939 TO 1952"),
            ("@#DGREGORIAN@ FROM 1939 TO 1952", "FROM 1939 TO 1952"),
            ("GREGORIAN FROM 1939", "FROM 1939"),
            ("TO GREGORIAN 1952", "TO 1952"),
            ("@#DGREGORIAN@ TO 1952", "TO 1952"),
            ("FROM ABT @#DGREGORIAN@ 1939 TO BEF GREGORIAN 1952",
             "FROM ABT 1939 TO BEF 1952"),
            ("FROM BET GREGORIAN 1938 AND GREGORIAN 1939 TO 1952",
             "FROM BET 1938 AND 1939 TO 1952"),
        ],
    )  # fmt: skip
    def test_a_period_is_the_same_start_and_end(self, tag, date, plain):
        expected = held_qualifiers(tag, plain)
        assert {prop for prop, _ in expected} & {"start_time", "end_time"}
        assert held_qualifiers(tag, date) == expected
        assert (
            adapt_gedcom(gustav(tag, date), artifact=EXPORT).warnings
            == adapt_gedcom(gustav(tag, plain), artifact=EXPORT).warnings
        )

    @pytest.mark.parametrize(
        ("date", "plain"),
        [
            ("@#DGREGORIAN@ FROM 1947 TO 1957", "FROM 1947 TO 1957"),
            ("FROM GREGORIAN 1947 TO @#DGREGORIAN@ 1957",
             "FROM 1947 TO 1957"),
            ("ABT @#DGREGORIAN@ 8 FEB 1947", "ABT 8 FEB 1947"),
        ],
    )  # fmt: skip
    def test_a_marriage_and_a_divorce_give_the_same_dates(self, date, plain):
        for event, written, written_plain in (
            ("f5-wedding", couple(marriage=date), couple(marriage=plain)),
            ("f5-divorce", couple(divorce=date), couple(divorce=plain)),
        ):
            expected = dates(written_plain, event)
            assert expected
            assert dates(written, event) == expected
            assert (
                adapt_gedcom(written, artifact=EXPORT).warnings
                == adapt_gedcom(written_plain, artifact=EXPORT).warnings
            )

    @pytest.mark.parametrize(
        "date",
        [
            # a date phrase is free text, the calendar in it or before it
            "(@#DGREGORIAN@ 12 MAR 1836)",
            "(GREGORIAN 12 MAR 1836)",
            "@#DGREGORIAN@ (Lady Day 1836)",
            "GREGORIAN (Lady Day 1836)",
            # the calendar alone, twice before one date, or as part of
            # another word
            "@#DGREGORIAN@",
            "GREGORIAN",
            "GREGORIAN GREGORIAN 1836",
            "@#DGREGORIAN@ @#DGREGORIAN@ 1836",
            "GREGORIAN @#DGREGORIAN@ 1836",
            "@#DGREGORIAN@ GREGORIAN 1836",
            "ABT GREGORIAN @#DGREGORIAN@ 1836",
            "GREGORIANX 1836",
            "XGREGORIAN 1836",
            # the keyword glued to the year makes another word
            "GREGORIAN1836",
        ],
    )
    def test_no_date_is_read_where_the_grammar_reads_none(self, date):
        assert parse_gedcom_date(date) == (None, [])
        result = adapt_gedcom(
            eugen("1 BIRT", f"2 DATE {date}"), artifact=EXPORT
        )
        assert on(result.batch, "i1", "date_of_birth") == []
        assert any("unparsed date" in w for w in result.warnings)
