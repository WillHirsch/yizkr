"""Birth input fidelity independently of matching or display preferences."""

from __future__ import annotations

import copy

import pytest

from yizkr.core.dates import GREGORIAN
from yizkr.pipeline.birth_evidence import BirthEvidence, from_rows

ROOT = "https://fictional.invalid"
PROPERTIES = {
    "stated_in": "P1",
    "page_number": "P2",
    "transcription_excerpt": "P3",
    "inferred_from_extraction_run": "P4",
}
ITEMS = {
    "circa": "Q50",
    "machine_assisted": "Q51",
    "combined_from_sources": "Q52",
}


def uri(value):
    return {"type": "uri", "value": value}


def literal(value, **metadata):
    return {"type": "literal", "value": str(value), **metadata}


def row(statement="birth-a", rank="NormalRank", **extra):
    return {
        "person": uri(ROOT + "/entity/Q1"),
        "statement": uri(ROOT + "/entity/statement/" + statement),
        "rank": uri("http://wikiba.se/ontology#" + rank),
        "time": literal("1876-03-19T00:00:00Z"),
        "timePrecision": literal(11),
        "timeCalendar": uri(GREGORIAN),
        **extra,
    }


def reference_rows(base, *, prefix="date", reference="r-a", source="Q10"):
    common = {
        **base,
        prefix + "Reference": uri(ROOT + "/reference/" + reference),
        prefix + "Source": uri(ROOT + "/entity/" + source),
        prefix + "Artifact": uri(ROOT + "/entity/Q20"),
    }
    return [
        {
            **common,
            prefix + "ReferenceProperty": uri(ROOT + "/entity/" + prop),
            prefix + "ReferenceValue": value,
        }
        for prop, value in (
            ("P1", uri(ROOT + "/entity/" + source)),
            ("P2", literal("2")),
            ("P3", literal("Fictional birth entry", **{"xml:lang": "en"})),
            ("P4", uri(ROOT + "/entity/Q30")),
        )
    ]


def test_competing_statements_ranks_and_reference_blocks_survive():
    first = reference_rows(row())
    second = reference_rows(
        row("birth-b", "PreferredRank", time=literal("1877-01-01T00:00:00Z")),
        reference="r-b",
        source="Q11",
    )
    # A second reference for one statement is not merged into the first
    # block, even when both cite the same source at different locations.
    other_location = reference_rows(row(), reference="r-c")
    for r in other_location:
        if r["dateReferenceProperty"]["value"].endswith("P2"):
            r["dateReferenceValue"] = literal("7")
    rows = first + second + other_location
    result = from_rows(rows, PROPERTIES, ITEMS)
    assert from_rows(reversed(rows + rows), PROPERTIES, ITEMS) == result
    births = result["Q1"]
    assert {birth.rank for birth in births} == {"normal", "preferred"}
    assert {birth.value.value for birth in births} == {
        "1876-03-19T00:00:00Z",
        "1877-01-01T00:00:00Z",
    }
    birth = next(birth for birth in births if birth.rank == "normal")
    assert len(birth.references) == 2
    pages = {
        ref.reference: tuple(
            snak.value.value
            for snak in ref.snaks
            if snak.property == "page_number"
        )
        for ref in birth.references
    }
    assert pages == {
        ROOT + "/reference/r-a": ("2",),
        ROOT + "/reference/r-c": ("7",),
    }
    for ref in birth.references:
        assert [(s.source, s.artifact) for s in ref.sources] == [
            ("Q10", "Q20")
        ]
        excerpt = next(
            s for s in ref.snaks if s.property == "transcription_excerpt"
        )
        assert excerpt.value.language == "en"
        assert any(
            s.property == "inferred_from_extraction_run" for s in ref.snaks
        )
    assert BirthEvidence.model_validate_json(birth.model_dump_json()) == birth


def test_bounds_are_plural_not_reduced_to_a_hull_or_a_preferred_reading():
    rows = [
        row(
            earliest=literal(stamp),
            earliestPrecision=literal(precision),
            earliestCalendar=uri(GREGORIAN),
            latest=literal("1880-01-01T00:00:00Z"),
            latestPrecision=literal(8),
            latestCalendar=uri(GREGORIAN),
            circumstance=uri(ROOT + "/entity/" + circumstance),
        )
        for stamp, precision in (
            ("1801-01-01T00:00:00Z", 7),
            ("1870-01-01T00:00:00Z", 9),
        )
        for circumstance in ("Q50", "Q51")
    ]
    (birth,) = from_rows(rows, PROPERTIES, ITEMS)["Q1"]
    assert {(v.value, v.precision) for v in birth.earliest} == {
        ("1801-01-01T00:00:00Z", 7),
        ("1870-01-01T00:00:00Z", 9),
    }
    assert birth.circumstances == ("circa", "machine_assisted")
    text = birth.facts()
    assert "earliest_date=the 19th century" in text
    assert "earliest_date=1870" in text
    assert "latest_date=the 1880s" in text
    assert "circa" in text and "machine_assisted" in text


@pytest.mark.parametrize("event", [False, True])
def test_curator_combination_keeps_its_method_and_separate_references(event):
    base = row(
        determinationMethod=uri(ROOT + "/entity/Q52"),
        **(
            {
                "event": uri(ROOT + "/entity/Q40"),
                "participation": uri(ROOT + "/entity/statement/principal"),
                "participationRank": uri(
                    "http://wikiba.se/ontology#NormalRank"
                ),
                "principalDeterminationMethod": uri(ROOT + "/entity/Q52"),
                "principalCircumstance": uri(ROOT + "/entity/Q51"),
            }
            if event
            else {}
        ),
    )
    rows = reference_rows(base) + reference_rows(
        base, reference="r-b", source="Q11"
    )
    # More than one method remains explicit, including an unrecognized
    # curator vocabulary item; duplicate joins do not create extra methods.
    other = [
        dict(r, determinationMethod=uri(ROOT + "/entity/Q99")) for r in rows
    ]
    result = from_rows(rows + other + rows, PROPERTIES, ITEMS)
    (birth,) = result["Q1"]
    assert birth.determination_methods == ("Q99", "combined_from_sources")
    assert {ref.sources[0].source for ref in birth.references} == {
        "Q10",
        "Q11",
    }
    text = birth.facts()
    assert "determination_method=Q99" in text
    assert "determination_method=combined_from_sources" in text
    assert "no cited source states the value alone" in text
    if event:
        assert birth.participation.determination_methods == (
            "combined_from_sources",
        )
        assert birth.participation.circumstances == ("machine_assisted",)
        assert "principal determination_method=combined_from_sources" in text
        assert "principal sourcing_circumstances=machine_assisted" in text
    assert BirthEvidence.model_validate_json(birth.model_dump_json()) == birth
    assert from_rows(reversed(rows + other), PROPERTIES, ITEMS) == result


@pytest.mark.parametrize(
    "date_rank,principal_rank",
    [("DeprecatedRank", "NormalRank"), ("NormalRank", "DeprecatedRank")],
)
def test_event_principal_keeps_separate_attribution_and_deprecation(
    date_rank, principal_rank
):
    base = row(
        "event-date",
        date_rank,
        event=uri(ROOT + "/entity/Q40"),
        participation=uri(ROOT + "/entity/statement/principal"),
        participationRank=uri("http://wikiba.se/ontology#" + principal_rank),
    )
    dates = reference_rows(base)
    principals = reference_rows(
        base, prefix="principal", reference="r-principal", source="Q12"
    )
    joined = [
        {**date, **principal} for date in dates for principal in principals
    ]
    (birth,) = from_rows(joined, PROPERTIES, ITEMS)["Q1"]
    assert birth.event == "Q40"
    assert birth.references[0].sources[0].source == "Q10"
    assert birth.participation.references[0].sources[0].source == "Q12"
    text = birth.facts()
    assert "birth event Q40 point_in_time" in text
    assert "principal=Q1" in text
    assert "not active identity evidence" in text
    assert "rank=deprecated" in text
    assert "Q10" in text and "Q12" in text


@pytest.mark.parametrize(
    "kind,shown",
    [
        ("somevalue", "unknown value (date not stated)"),
        ("novalue", "no value"),
    ],
)
def test_unknown_and_no_value_dates_are_explicit(kind, shown):
    given = row(valueKind=literal(kind))
    for key in ("time", "timePrecision", "timeCalendar"):
        given.pop(key)
    (birth,) = from_rows([given], PROPERTIES, ITEMS)["Q1"]
    assert birth.value is None
    assert birth.value_kind == kind
    assert shown in birth.facts()
    assert "references: none recorded" in birth.facts()


@pytest.mark.parametrize("name", ["time", "earliest", "dateReferenceTime"])
def test_unsupported_calendar_in_any_retained_date_refuses(name):
    given = reference_rows(row())[0]
    given.update(
        {
            name: literal("1876-03-19T00:00:00Z"),
            name + "Precision": literal(11),
            name + "Calendar": uri("http://www.wikidata.org/entity/Q1985786"),
        }
    )
    with pytest.raises(ValueError, match="Gregorian"):
        from_rows([given], PROPERTIES, ITEMS)


def test_conflicting_rows_do_not_sample_a_date():
    one = row()
    two = copy.deepcopy(one)
    two["time"] = literal("1877-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="inconsistent rows"):
        from_rows([one, two], PROPERTIES, ITEMS)


def test_unknown_rank_and_missing_time_do_not_become_normal_evidence():
    with pytest.raises(ValueError, match="unknown rank"):
        from_rows([row(rank="OtherRank")], PROPERTIES, ITEMS)
    given = row()
    given.pop("time")
    with pytest.raises(ValueError, match="time value"):
        from_rows([given], PROPERTIES, ITEMS)
