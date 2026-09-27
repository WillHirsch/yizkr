"""Invariants of the fictional example universe (#40, docs/EXAMPLES.md).

Offline; no instance, no LLM. These pin the patterns examples rely on, so
the universe cannot quietly lose one while it grows.
"""

from collections import Counter
from datetime import date, timedelta
from itertools import combinations

import pytest

from tests.fixtures import example_universe as u
from yizkr.model.schema import load_manifest
from yizkr.pipeline.gedcom import parse_gedcom, parse_gedcom_date

KEYS = u.by_key()
PEOPLE = {person.key: person for person in u.PEOPLE}
PLACES = {place.key: place for place in u.PLACES}
ENTITIES = {entity.key: entity for entity in u.ENTITIES}


def values(subject: str, prop: str) -> list:
    return [assertion.value for assertion in u.about(subject, prop)]


def _span(when: u.Date) -> tuple[date, date]:
    if when.decade:
        return date(when.year, 1, 1), date(when.year + 9, 12, 31)
    if when.day:
        day = date(when.year, when.month, when.day)
        return day, day
    if when.month:
        first = date(when.year, when.month, 1)
        following = date(when.year + when.month // 12, when.month % 12 + 1, 1)
        return first, following - timedelta(days=1)
    return date(when.year, 1, 1), date(when.year, 12, 31)


def bounds(when: u.Date) -> tuple[date, date]:
    """Earliest and latest day a date allows; circa never narrows."""
    low, high = _span(when)
    if when.qualifier == "before":
        return date.min, high
    if when.qualifier == "after":
        return low, date.max
    if when.qualifier == "between":
        return low, _span(when.until)[1]
    if when.qualifier == "circa":
        return date.min, date.max
    return low, high


def overlap(first: u.Date, second: u.Date) -> bool:
    (low_a, high_a), (low_b, high_b) = bounds(first), bounds(second)
    return low_a <= high_b and low_b <= high_a


class TestIntegrity:
    def test_keys_are_unique(self):
        keys = Counter(
            obj.key for obj in (*u.PEOPLE, *u.PLACES, *u.ENTITIES, *u.SOURCES)
        )
        assert [key for key, n in keys.items() if n > 1] == []

    def test_assertions_follow_the_manifest(self):
        manifest = load_manifest()
        sources = {source.key for source in u.SOURCES}
        for assertion in u.ASSERTIONS:
            assert assertion.subject in KEYS, assertion
            assert assertion.source in sources, assertion
            for prop, value in (
                (assertion.property, assertion.value),
                *assertion.qualifiers,
            ):
                datatype = manifest.properties[prop].datatype
                if datatype == "wikibase-item":
                    assert value in KEYS, (prop, value)
                elif datatype == "time":
                    assert isinstance(value, u.Date), (prop, value)
                elif datatype == "quantity":
                    assert isinstance(value, int), (prop, value)
                else:
                    assert isinstance(value, str) and value, (prop, value)

    def test_kinds_are_manifest_items(self):
        items = load_manifest().items
        for entity in u.ENTITIES:
            assert items[entity.kind].entity_kind, entity.key
        for source in u.SOURCES:
            if source.artifact_class:
                assert source.artifact_class in items, source.key
                assert not items[source.artifact_class].entity_kind

    def test_everyone_but_the_placeholder_is_attested(self):
        mentioned = {a.subject for a in u.ASSERTIONS} | {
            a.value for a in u.ASSERTIONS if isinstance(a.value, str)
        }
        assert set(PEOPLE) - mentioned == {"placeholder"}


class TestNames:
    def test_birth_married_and_nickname_on_one_person(self):
        typed = {"birth_name", "married_name", "nickname"}
        assert {
            key
            for key in PEOPLE
            if typed <= {a.property for a in u.about(key)}
        }

    def test_german_and_english_forms(self):
        crossed = [
            person.key
            for person in u.PEOPLE
            if any(n.form == "birth" and n.lang == "de" for n in person.names)
            and any(
                n.form in {"anglicised", "official"} and n.lang == "en"
                for n in person.names
            )
        ]
        assert len(crossed) >= 3
        nicknames = {
            n.lang
            for person in u.PEOPLE
            for n in person.names
            if n.form == "nickname"
        }
        assert {"de", "en"} <= nicknames

    @pytest.mark.parametrize(
        "marker", ["/", "(b. ", "(later ", ") ", " jun.", " sen.", "G. "]
    )
    def test_composite_forms_as_sources_write_them(self, marker):
        written = [
            n.text
            for person in u.PEOPLE
            for n in person.names
            if n.form == "as_written"
        ]
        assert any(marker in text for text in written), marker

    def test_homonyms_are_distinguishable(self):
        by_label = Counter(person.label for person in u.PEOPLE)
        homonyms = {
            frozenset(p.key for p in u.PEOPLE if p.label == label)
            for label, n in by_label.items()
            if n > 1
        }
        assert frozenset({"gustav_1851", "gustav_1925"}) in homonyms
        assert frozenset({"hugo_sen", "hugo_jun"}) in homonyms
        # the Gustavs by disjoint lifespans, the Hugos by a father edge
        elder_death = values("gustav_1851", "date_of_death")[0]
        younger_birth = values("gustav_1925", "date_of_birth")[0]
        assert bounds(elder_death)[1] < bounds(younger_birth)[0]
        assert "hugo_sen" in values("hugo_jun", "father")


class TestFamily:
    def test_parents_span_five_generations(self):
        line, key = [], "nora"
        while fathers := values(key, "father"):
            key = fathers[0]
            line.append(key)
        assert line == ["gustav_1925", "hugo_jun", "hugo_sen", "gustav_1851"]

    def test_generation_numbers_follow_parent_edges(self):
        for assertion in u.ASSERTIONS:
            if assertion.property in {"father", "mother"}:
                child = PEOPLE[assertion.subject]
                parent = PEOPLE[assertion.value]
                assert child.generation == parent.generation + 1, assertion

    def test_spouses_including_an_ended_marriage(self):
        marriages = [a for a in u.ASSERTIONS if a.property == "spouse"]
        assert len({(a.subject, a.value) for a in marriages}) >= 6
        assert any("end_time" in dict(a.qualifiers) for a in marriages), (
            "a divorce"
        )

    def test_family_firm_employs_family_members(self):
        firm = ENTITIES["gustavmuehlen_ag"]
        assert firm.kind == "organization"
        staff = {
            a.subject
            for a in u.ASSERTIONS
            if a.property == "employer" and a.value == firm.key
        }
        assert staff >= {"hugo_sen", "hugo_jun"}
        assert values(firm.key, "location") == ["old_mill"]


class TestPlaces:
    def test_street_town_region_country_chain(self):
        (path,) = u.containment_paths("muehlgasse_12")
        assert [PLACES[key].kind for key in path] == [
            "address", "town", "region", "country",
        ]  # fmt: skip

    def test_every_place_reaches_a_country(self):
        for key in PLACES:
            for path in u.containment_paths(key):
                assert len(path) == len(set(path)), path  # no cycles
                assert PLACES[path[-1]].kind == "country", path

    def test_eponymous_nesting(self):
        assert any(
            PLACES[parent].label == place.label
            for place in u.PLACES
            for parent in place.part_of
        )

    def test_one_town_under_two_naming_conventions(self):
        assert len(PLACES["sampleton"].part_of) == 2


class TestEventsAndDates:
    @pytest.mark.parametrize(
        "kind", ["deportation", "imprisonment", "release"]
    )
    def test_persecution_events_are_events(self, kind):
        events = [e.key for e in u.ENTITIES if e.kind == kind]
        assert events
        for key in events:
            people = values(key, "participant")
            assert people and all(p in PEOPLE for p in people)
            (where,) = values(key, "location")
            assert PLACES[where].kind in {"camp", "prison"}
            assert values(key, "point_in_time") or values(key, "start_time")

    @pytest.mark.parametrize(("subject", "prop"), u.CONTRADICTIONS)
    def test_contradictions_across_two_sources(self, subject, prop):
        assertions = u.about(subject, prop)
        assert len({a.source for a in assertions}) >= 2
        assert any(
            not overlap(first.value, second.value)
            for first, second in combinations(assertions, 2)
        )

    @pytest.mark.parametrize(("subject", "prop"), u.REFINEMENTS)
    def test_refinements_are_not_contradictions(self, subject, prop):
        found = values(subject, prop)
        assert len(found) >= 2
        for first, second in combinations(found, 2):
            if isinstance(first, u.Date):
                assert overlap(first, second)
            else:
                paths = u.containment_paths(first) + u.containment_paths(
                    second
                )
                assert any(first in p and second in p for p in paths)

    def test_uncertain_dates_cover_every_form(self):
        dates = [
            value
            for a in u.ASSERTIONS
            for value in (a.value, *dict(a.qualifiers).values())
            if isinstance(value, u.Date)
        ]
        assert {d.qualifier for d in dates} >= {
            "circa", "before", "after", "between",
        }  # fmt: skip
        assert {d.precision for d in dates} >= {
            "day", "month", "year", "decade",
        }  # fmt: skip

    @pytest.mark.parametrize(
        ("when", "qualifiers"),
        [
            (u.Date(1880, qualifier="circa"), {"sourcing_circumstances"}),
            (u.Date(1945, 5, 8, qualifier="before"), {"latest_date"}),
            (u.Date(1942, 10, qualifier="after"), {"earliest_date"}),
            (
                u.Date(1910, qualifier="between", until=u.Date(1912)),
                {"earliest_date", "latest_date"},
            ),
            (u.Date(1850, decade=True), {"earliest_date", "latest_date"}),
        ],
    )
    def test_gedcom_rendering_keeps_the_uncertainty(self, when, qualifiers):
        value, quals = parse_gedcom_date(when.gedcom)
        assert value["year"] == when.year
        assert {q["property"] for q in quals} == qualifiers

    def test_exactly_one_possibly_living_person(self):
        # born, and no source asserts a death: the privacy example
        living = [
            key
            for key in PEOPLE
            if values(key, "date_of_birth")
            and not values(key, "date_of_death")
        ]
        assert living == ["nora"]


class TestRelationalAppellations:
    def test_one_term_literal_for_one_person_courtesy_for_another(self):
        literal = [
            a
            for a in u.ASSERTIONS
            if a.property == "relative"
            and dict(a.qualifiers).get("kinship_type") == "Oma"
        ]
        courtesy = [
            a
            for a in u.ASSERTIONS
            if a.property == "referred_to_as"
            and a.value.startswith("Oma ")
            and "perspective" in dict(a.qualifiers)
        ]
        assert literal and courtesy
        assert PEOPLE[courtesy[0].subject].generation is None  # no kin


@pytest.fixture(scope="module")
def records():
    return parse_gedcom(u.GEDCOM_FIXTURE.read_text())


class TestGedcomFixture:
    """example-family.ged is an excerpt of the universe's family tree."""

    def test_every_individual_is_a_universe_person(self, records):
        by_xref = {p.gedcom_xref: p for p in u.PEOPLE if p.gedcom_xref}
        individuals = [r for r in records if r.tag == "INDI"]
        assert {r.xref for r in individuals} == set(by_xref)
        sexes = {"M": "male", "F": "female", "": None}
        for record in individuals:
            assert by_xref[record.xref].sex == sexes[record.text("SEX")]

    def test_families_match_the_tree(self, records):
        by_xref = {p.gedcom_xref: p.key for p in u.PEOPLE if p.gedcom_xref}
        tree = [a for a in u.ASSERTIONS if a.source == "family_tree_1990"]
        facts = {(a.subject, a.property, a.value) for a in tree}
        for family in (r for r in records if r.tag == "FAM"):
            husband = by_xref[family.text("HUSB")]
            wife = by_xref[family.text("WIFE")]
            assert (husband, "spouse", wife) in facts or (
                wife,
                "spouse",
                husband,
            ) in facts
            for child in family.all("CHIL"):
                assert (by_xref[child.value], "father", husband) in facts
                assert (by_xref[child.value], "mother", wife) in facts
