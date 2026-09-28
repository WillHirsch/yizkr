"""Offline tests for the GEDCOM adapter (item 13).

The fixture is the example universe's GEDCOM rendering and every inline
snippet uses the same universe or bare placeholders (docs/EXAMPLES.md).
"""

from pathlib import Path

import pytest

from yizkr.claims import validate_against_manifest
from yizkr.pipeline.gedcom import (
    ADAPTER_IDENTIFIER,
    adapt_gedcom,
    format_unsupported_tags,
    parse_gedcom,
    parse_gedcom_date,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "example-family.ged"


def fixture_result():
    return adapt_gedcom(FIXTURE.read_text(), artifact_qid="Q23")


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


class TestAdapter:
    def test_batch_is_manifest_valid_and_deterministic(self):
        first = fixture_result()
        assert validate_against_manifest(first.batch) == []
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
        assert caecilie.aliases == ["Cäcilie Löbig"]
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
        (marriage,) = [
            c
            for c in claims_for(batch, "i3", "spouse")
            if c.value.value.local_id == "i4"
        ]
        quals = {q.property for q in marriage.qualifiers}
        assert {"start_time", "location"} <= quals

    def test_divorce_becomes_end_time(self):
        # Leopold (i11) and Agnes (i17): married 8 Feb 1947, divorced
        # 2 Jan 1957; each spouse claim carries both dates
        batch = fixture_result().batch
        for subject, other in (("i11", "i17"), ("i17", "i11")):
            (spouse,) = claims_for(batch, subject, "spouse")
            assert spouse.value.value.local_id == other
            quals = {q.property: q.value for q in spouse.qualifiers}
            start, end = quals["start_time"], quals["end_time"]
            assert (start.year, start.month, start.day) == (1947, 2, 8)
            assert (end.year, end.month, end.day) == (1957, 1, 2)

    def test_every_claim_carries_verbatim_gedcom_excerpt(self):
        batch = fixture_result().batch
        for claim in batch.claims:
            excerpt = claim.source.transcription_excerpt
            assert claim.source.artifact.qid == "Q23"
            assert excerpt is not None and len(excerpt.text) <= 400
            # monolingual text values forbid newlines
            assert "\n" not in excerpt.text
        (birth,) = claims_for(batch, "i3", "date_of_birth")
        assert "2 DATE 15 FEB 1862" in birth.source.transcription_excerpt.text

    def test_unparseable_date_warns_instead_of_guessing(self):
        result = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 BIRT\n2 DATE SOMEDAY MAYBE\n"
            "0 TRLR\n",
            artifact_qid="Q23",
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
            artifact_qid="Q23",
        )
        assert any("BURI without PLAC" in w for w in result.warnings)
        assert not claims_for(result.batch, "i1", "burial_place")

    def test_dated_and_placed_occupation_carries_qualifiers(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Sebastian /Löbig/\n1 OCCU Hatter\n"
            "2 DATE 1919\n2 PLAC Flörburg\n0 TRLR\n",
            artifact_qid="Q23",
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
            artifact_qid="Q23",
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

    def test_inline_parenthetical_nickname_extracted(self):
        # "Ida (Peppi) Sörne" -> nickname "Peppi", label keeps it all
        batch = fixture_result().batch
        assert self._name(batch, "i7", "nickname") == ["Peppi"]
        ida = next(e for e in batch.entities if e.id == "i7")
        assert ida.label == "Ida (Peppi) Sörne"

    def test_name_type_immigrant_is_official(self):
        batch = fixture_result().batch
        assert self._name(batch, "i8", "official_name") == ["Gustave Lobig"]

    def test_name_type_official(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Gustav /Löbig/\n"
            "1 NAME Gustave /Lobig/\n2 TYPE official\n0 TRLR\n",
            artifact_qid="Q23",
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
                assert claim.source.artifact.qid == "Q23"
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
        # label/alias match index
        batch = fixture_result().batch
        caecilie = next(e for e in batch.entities if e.id == "i2")
        assert caecilie.label == "Cäcilie Tarle"
        assert caecilie.aliases == ["Cäcilie Löbig"]

    def test_explicit_type_gedcom_maiden_and_aka(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME Hildegard /Laudler/\n2 TYPE maiden\n"
            "1 NAME Hilde /Löbig/\n2 TYPE aka\n0 TRLR\n",
            artifact_qid="Q23",
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
        batch = adapt_gedcom(RESI_GEDCOM, artifact_qid="Q23").batch
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
        batch = adapt_gedcom(RESI_GEDCOM, artifact_qid="Q23").batch
        undated = [
            c for c in claims_for(batch, "i1", "residence") if not c.qualifiers
        ]
        # the unparsed-date residence also ends up bare (date omitted)
        assert len(undated) == 2

    def test_unparsed_residence_date_warns_and_omits(self):
        result = adapt_gedcom(RESI_GEDCOM, artifact_qid="Q23")
        assert any(
            "SOMEDAY MAYBE" in w and "point_in_time omitted" in w
            for w in result.warnings
        )

    def test_residence_without_place_warns_and_skips(self):
        result = adapt_gedcom(RESI_GEDCOM, artifact_qid="Q23")
        assert any("RESI without PLAC" in w for w in result.warnings)
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
        tags = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").unsupported_tags
        # REPO and EDUC are genuinely unmapped, at level 0 and nested — both
        # reported (the old direct-children scan would have missed REPO)
        assert tags["REPO"] == 1
        assert tags["EDUC"] == 1
        # SOUR and OBJE are now incorporated (ADR-0021), so the census no
        # longer flags them — as records, pointers, or nested citations
        assert "SOUR" not in tags and "OBJE" not in tags

    def test_handled_and_dropped_subtrees_are_not_itemised(self):
        # a handled/dropped tag stands for its whole branch: the citation's
        # PAGE/_APID and the media object's FILE/TITL are never itemised
        tags = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").unsupported_tags
        assert "PAGE" not in tags and "_APID" not in tags
        assert "TITL" not in tags and "FILE" not in tags

    def test_name_parts_and_envelope_are_not_dropped(self):
        tags = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").unsupported_tags
        # GIVN/SURN restate the NAME value the adapter reads — not dropped
        assert "GIVN" not in tags and "SURN" not in tags
        # HEAD/TRLR envelope (and the header's own CHAR) never counted
        assert "HEAD" not in tags and "TRLR" not in tags
        assert "CHAR" not in tags


class TestSourceCitations:
    """SOUR citations fold into the claim's reference (ADR-0021 step A)."""

    def test_page_becomes_the_reference_page_number(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").batch
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        assert birth.source.page_number == "Vol 3 p 1"

    def test_cited_source_title_surfaces_in_excerpt(self):
        # the verbatim lines carry only "@S1@"; the resolved upstream source
        # name is appended so the reviewer can read the citation
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").batch
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        excerpt = birth.source.transcription_excerpt.text
        assert "cited: Birth register, Standesamt Flörburg" in excerpt

    def test_claim_without_a_citation_has_no_page_number(self):
        batch = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 BIRT\n2 DATE 1900\n0 TRLR\n",
            artifact_qid="Q23",
        ).batch
        (birth,) = claims_for(batch, "i1", "date_of_birth")
        assert birth.source.page_number is None


class TestMediaObjects:
    """OBJE becomes a lightweight un-held media item (ADR-0021)."""

    def test_person_images_an_unheld_media_item(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").batch
        (image,) = claims_for(batch, "i1", "image")
        media = next(
            e for e in batch.entities if e.id == image.value.value.local_id
        )
        assert media.kind == "artifact"  # not photograph: bytes not held
        assert media.label == "School report for Rudolf Löbig"
        assert "never fetched" in media.summary

    def test_media_association_carries_the_objects_date(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").batch
        (image,) = claims_for(batch, "i1", "image")
        (qualifier,) = image.qualifiers
        assert qualifier.property == "point_in_time"
        assert qualifier.value.year == 1930

    def test_batch_stays_manifest_valid(self):
        batch = adapt_gedcom(CENSUS_GEDCOM, artifact_qid="Q23").batch
        assert validate_against_manifest(batch) == []

    def test_shared_object_is_one_media_item(self):
        gedcom = (
            "0 @O1@ OBJE\n1 FILE studio-portrait-1890s.jpg\n"
            "2 TITL Studio portrait of the Löbig family\n"
            "0 @I1@ INDI\n1 NAME Eugen /Löbig/\n1 OBJE @O1@\n"
            "0 @I2@ INDI\n1 NAME Cäcilie /Tarle/\n1 OBJE @O1@\n"
            "0 TRLR\n"
        )
        batch = adapt_gedcom(gedcom, artifact_qid="Q23").batch
        media = [e for e in batch.entities if e.kind == "artifact"]
        assert len(media) == 1  # deduped by xref across both people
        assert len(claims_for(batch, "i1", "image")) == 1
        assert len(claims_for(batch, "i2", "image")) == 1

    def test_dangling_pointer_warns_and_skips(self):
        result = adapt_gedcom(
            "0 @I1@ INDI\n1 NAME A /B/\n1 OBJE @O9@\n0 TRLR\n",
            artifact_qid="Q23",
        )
        assert any(
            "OBJE points to unknown object" in w for w in result.warnings
        )
        assert not claims_for(result.batch, "i1", "image")


class TestUnsupportedFormat:
    def test_count_is_kept_apart_from_tag(self):
        # "OBJEx5" would read as a corrupt tag; the count must be legible
        assert (
            format_unsupported_tags({"OBJE": 5, "SOUR": 12})
            == "OBJE ×5, SOUR ×12"
        )

    def test_empty_census_is_empty_string(self):
        assert format_unsupported_tags({}) == ""


def couple(marriage: str | None = None, divorce: str | None = None) -> str:
    """Leopold and Agnes's family record, its marriage and divorce dated
    (here) as given."""
    lines = [
        "0 @I11@ INDI",
        "1 NAME Leopold /Löbig/",
        "0 @I17@ INDI",
        "1 NAME Agnes /Löbig/",
        "0 @F5@ FAM",
        "1 HUSB @I11@",
        "1 WIFE @I17@",
    ]
    if marriage is not None:
        lines += ["1 MARR", f"2 DATE {marriage}"]
    if divorce is not None:
        lines += ["1 DIV", f"2 DATE {divorce}"]
    return "\n".join([*lines, "0 TRLR", ""])


def shown(value) -> tuple | str:
    """A qualifier's value: a time as (year, month, day, precision), a
    vocabulary item by its key."""
    if value.type == "time":
        return value.year, value.month, value.day, value.precision
    return value.value.key


def spouse_qualifiers(text: str) -> list[tuple]:
    """The qualifiers of the couple's spouse claims, which agree, as
    (property, value) pairs in property order."""
    batch = adapt_gedcom(text, artifact_qid="Q23").batch
    found = []
    for subject in ("i11", "i17"):
        (spouse,) = claims_for(batch, subject, "spouse")
        found.append(
            sorted((q.property, shown(q.value)) for q in spouse.qualifiers)
        )
    assert found[0] == found[1]
    return found[0]


def year(number: int) -> tuple:
    return number, None, None, "year"


class TestMarriageDates:
    """A marriage's date is its spouse claims' start time and a divorce's
    their end time, each with the range its date states as the bounds of
    that end (docs/MODELING.md, "Marriages, residences and other
    periods")."""

    def test_no_spouse_claim_carries_a_point_in_time(self):
        spouses = [
            claim
            for claim in fixture_result().batch.claims
            if claim.property == "spouse"
        ]
        assert spouses
        for claim in spouses:
            assert "point_in_time" not in {
                q.property for q in claim.qualifiers
            }

    def test_a_marriage_date_is_the_start_time(self):
        # Sebastian and Hildegard, married on 15 December 1905
        batch = fixture_result().batch
        (marriage,) = [
            c
            for c in claims_for(batch, "i3", "spouse")
            if c.value.value.local_id == "i4"
        ]
        (start,) = [
            q for q in marriage.qualifiers if q.property == "start_time"
        ]
        assert shown(start.value) == (1905, 12, 15, "day")

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "BET 1946 AND 1947",
                [
                    ("earliest_date", year(1946)),
                    ("latest_start_date", year(1947)),
                    ("start_time", year(1946)),
                ],
            ),
            (
                "BEF 1947",
                [
                    ("latest_start_date", year(1947)),
                    ("start_time", year(1947)),
                ],
            ),
            (
                "AFT 1946",
                [("earliest_date", year(1946)), ("start_time", year(1946))],
            ),
            (
                "ABT 1947",
                [
                    ("sourcing_circumstances", "circa"),
                    ("start_time", year(1947)),
                ],
            ),
        ],
    )
    def test_a_marriage_range_bounds_the_start(self, date, expected):
        assert spouse_qualifiers(couple(marriage=date)) == expected

    @pytest.mark.parametrize(
        ("date", "expected"),
        [
            (
                "BET 1956 AND 1957",
                [
                    ("earliest_end_date", year(1956)),
                    ("end_time", year(1956)),
                    ("latest_date", year(1957)),
                ],
            ),
            (
                "AFT 1956",
                [("earliest_end_date", year(1956)), ("end_time", year(1956))],
            ),
            (
                "BEF 1957",
                [("end_time", year(1957)), ("latest_date", year(1957))],
            ),
            (
                "ABT 1957",
                [
                    ("end_time", year(1957)),
                    ("sourcing_circumstances", "circa"),
                ],
            ),
        ],
    )
    def test_a_divorce_range_bounds_the_end(self, date, expected):
        # the range and the circa a divorce date states are never dropped
        assert spouse_qualifiers(couple(divorce=date)) == expected

    def test_each_end_keeps_its_own_range(self):
        # (here) married "between 1946 and 1947" and divorced "after 1956":
        # no bound of one end is read as the other's
        assert spouse_qualifiers(couple("BET 1946 AND 1947", "AFT 1956")) == [
            ("earliest_date", year(1946)),
            ("earliest_end_date", year(1956)),
            ("end_time", year(1956)),
            ("latest_start_date", year(1947)),
            ("start_time", year(1946)),
        ]

    def test_one_circa_stands_for_two_approximate_ends(self):
        # (here) married about 1947 and divorced about 1957
        assert spouse_qualifiers(couple("ABT 1947", "ABT 1957")) == [
            ("end_time", year(1957)),
            ("sourcing_circumstances", "circa"),
            ("start_time", year(1947)),
        ]

    def test_an_undated_marriage_and_divorce_keep_the_wedding_place(self):
        # (here) the family record names where Leopold and Agnes married and
        # where they were divorced, but dates neither
        result = adapt_gedcom(
            "0 @I11@ INDI\n1 NAME Leopold /Löbig/\n"
            "0 @I17@ INDI\n1 NAME Agnes /Löbig/\n"
            "0 @F5@ FAM\n1 HUSB @I11@\n1 WIFE @I17@\n"
            "1 MARR\n2 PLAC Flörburg, Hesse, Germany\n"
            "1 DIV\n2 PLAC Rekau, Hesse, Germany\n"
            "0 TRLR\n",
            artifact_qid="Q23",
        )
        assert result.warnings == []
        labels = {e.id: e.label for e in result.batch.entities}
        for subject in ("i11", "i17"):
            (spouse,) = claims_for(result.batch, subject, "spouse")
            assert not {"start_time", "end_time"} & {
                q.property for q in spouse.qualifiers
            }
            (place,) = [
                q.value.value.local_id
                for q in spouse.qualifiers
                if q.property == "location"
            ]
            assert labels[place] == "Flörburg, Hesse, Germany"

    def test_an_unparsed_date_warns_and_omits_its_end(self):
        result = adapt_gedcom(
            couple("SOMEDAY MAYBE", "NEVER EVER"), artifact_qid="Q23"
        )
        assert any(
            "marriage date 'SOMEDAY MAYBE' — start_time omitted" in warning
            for warning in result.warnings
        )
        assert any(
            "divorce date 'NEVER EVER' — end_time omitted" in warning
            for warning in result.warnings
        )
        (spouse,) = claims_for(result.batch, "i11", "spouse")
        assert spouse.qualifiers == []
