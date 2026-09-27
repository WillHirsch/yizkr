"""Invariants of the fictional example universe (#40, docs/EXAMPLES.md).

Offline; no instance, no LLM.  These pin the patterns examples rely on,
so the universe cannot quietly lose one while it grows, and they check
that the GEDCOM rendering says nothing the universe's sources do not.
"""

import re
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
SOURCES = {source.key: source for source in u.SOURCES}

PERSECUTION_KINDS = ("event", "imprisonment", "release", "deportation")


def values(subject: str, prop: str) -> list:
    return [assertion.value for assertion in u.about(subject, prop)]


def dates() -> list[u.Date]:
    return [
        value
        for a in u.ASSERTIONS
        for value in (a.value, *dict(a.qualifiers).values())
        if isinstance(value, u.Date)
    ]


def _span(when: u.Date) -> tuple[date, date]:
    """The days a plain (unqualified) date covers at its precision."""
    if when.decade:
        return date(when.year, 1, 1), date(when.year + 9, 12, 31)
    if when.day:
        day = date(when.year, when.month, when.day)
        return day, day
    if when.month:
        first = date(when.year, when.month, 1)
        next_month = date(when.year + when.month // 12, when.month % 12 + 1, 1)
        return first, next_month - timedelta(days=1)
    return date(when.year, 1, 1), date(when.year, 12, 31)


def bounds(when: u.Date) -> tuple[date, date]:
    """Earliest and latest day a date allows.  Circa is never narrowed
    to its stated span: it may lie outside it."""
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
        for assertion in u.ASSERTIONS:
            assert assertion.subject in KEYS, assertion
            assert assertion.source in SOURCES, assertion
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

    def test_every_excerpt_is_verbatim_source_text(self):
        for assertion in u.ASSERTIONS:
            text = SOURCES[assertion.source].text
            assert assertion.excerpt in text, assertion

    def test_every_person_is_attested(self):
        mentioned = {a.subject for a in u.ASSERTIONS} | {
            a.value for a in u.ASSERTIONS if isinstance(a.value, str)
        }
        assert set(PEOPLE) <= mentioned

    def test_dates_are_well_formed(self):
        for when in dates():
            _span(when)  # raises on an impossible day or month
            if when.decade:
                assert when.year % 10 == 0 and not when.month, when
            if when.qualifier == "between":
                assert when.until and _span(when.until)[0] > _span(when)[0]
            else:
                assert when.until is None, when
            qualifiers = {None, "circa", "before", "after", "between"}
            assert when.qualifier in qualifiers, when


class TestNames:
    def test_birth_married_and_nickname_on_one_person(self):
        typed = {"birth_name", "married_name", "nickname"}
        assert {
            key
            for key in PEOPLE
            if typed <= {a.property for a in u.about(key)}
        } >= {"caecilie-loebig", "emma-loebig", "rebekka-loebig"}

    def test_german_and_english_forms_and_diminutives(self):
        crossed = [
            person.key
            for person in u.PEOPLE
            if any(n.form == "birth" and n.lang == "de" for n in person.names)
            and any(n.form == "anglicised" for n in person.names)
        ]
        assert crossed == ["gustav-loebig", "anna-loebig"]
        diminutives = {
            n.lang
            for person in u.PEOPLE
            for n in person.names
            if n.form == "diminutive"
        }
        assert diminutives == {"de", "en"}

    def test_asserted_nicknames_are_known_name_forms(self):
        for assertion in u.ASSERTIONS:
            if assertion.property == "nickname":
                forms = {
                    n.text: n.form for n in PEOPLE[assertion.subject].names
                }
                assert forms.get(assertion.value) in {
                    "nickname",
                    "diminutive",
                }, assertion

    def test_names_as_written_occur_in_a_source(self):
        texts = [source.text for source in u.SOURCES]
        written = [
            n.text
            for person in u.PEOPLE
            for n in person.names
            if n.form == "as_written"
        ]
        assert len(written) >= 4
        for text in written:
            assert any(text in source for source in texts), text

    def test_homonyms_are_distinguishable(self):
        by_label = Counter(person.label for person in u.PEOPLE)
        (label,) = [label for label, n in by_label.items() if n > 1]
        namesakes = [p for p in u.PEOPLE if p.label == label]
        assert {p.key for p in namesakes} == {
            "frieda-loebig",
            "frieda-loebig-angerring",
        }
        # one is family (parents, a generation), the other is not
        assert values("frieda-loebig", "father") == ["sebastian-loebig"]
        assert PEOPLE["frieda-loebig-angerring"].generation is None
        assert not values("frieda-loebig-angerring", "father")
        # the directory lists the widow when the other Frieda is a baby
        (listed,) = u.about("frieda-loebig-angerring", "residence")
        listed_in = dict(listed.qualifiers)["point_in_time"]
        (born,) = values("frieda-loebig", "date_of_birth")
        assert listed_in.year - born.year == 1

    def test_a_person_without_name_or_sex(self):
        infant = PEOPLE["infant-loebig"]
        assert infant.sex is None
        assert [n.form for n in infant.names] == ["placeholder"]


class TestFamily:
    def test_parents_span_four_generations(self):
        line, key = [], "nicholas-lobig"
        while fathers := values(key, "father"):
            key = fathers[0]
            line.append(key)
        assert line == ["gustav-loebig", "sebastian-loebig", "eugen-loebig"]

    def test_generation_numbers_follow_parent_edges(self):
        for assertion in u.ASSERTIONS:
            if assertion.property in {"father", "mother"}:
                child = PEOPLE[assertion.subject]
                parent = PEOPLE[assertion.value]
                assert child.generation == parent.generation + 1, assertion

    def test_spouses_in_three_generations(self):
        couples = {
            frozenset((a.subject, a.value))
            for a in u.ASSERTIONS
            if a.property == "spouse"
        }
        generations = {
            PEOPLE[next(iter(couple))].generation for couple in couples
        }
        assert len(couples) == 4
        assert generations == {1, 2, 3}

    def test_family_firm(self):
        firm = ENTITIES["zum-blauen-hut"]
        assert firm.kind == "organization"
        assert set(values(firm.key, "location")) == {"brunnenring-35"}
        staff = {
            a.subject
            for a in u.ASSERTIONS
            if a.property == "employer" and a.value == firm.key
        }
        assert staff == {"sebastian-loebig"}
        # the son owns it in 1912, after his father's death
        deaths = values("eugen-loebig", "date_of_death")
        assert deaths and all(when.year < 1912 for when in deaths)
        assert values(firm.key, "start_time") == [u.Date(1863)]
        (ended,) = values(firm.key, "end_time")
        assert ended.qualifier == "before"
        # the family trade, in English and German
        hatter = ENTITIES["hatter"]
        assert (hatter.label, hatter.names) == ("Hatter", ("Hutmacher",))


class TestPlaces:
    def test_address_street_town_region_country_chain(self):
        (path,) = u.containment_paths("brunnenring-35")
        assert [PLACES[key].kind for key in path] == [
            "address", "street", "town", "district", "region", "country",
        ]  # fmt: skip
        (path,) = u.containment_paths("hawthorn-road-8")
        assert [PLACES[key].kind for key in path] == [
            "address", "street", "town", "region", "country",
        ]  # fmt: skip

    def test_every_place_reaches_a_country_but_one(self):
        unlocated = set()
        for key in PLACES:
            for path in u.containment_paths(key):
                assert len(path) == len(set(path)), path  # no cycles
                if PLACES[path[-1]].kind != "country":
                    unlocated.add(path[-1])
        # the camp's locality: no source says where it lay
        assert unlocated == {"klurnau"}

    def test_a_town_named_two_ways_over_time(self):
        forms = {n.form: n.text for n in PLACES["floerburg"].names}
        assert forms["historical"] == "Flörburg"
        assert forms["official"] == "Bad Flörburg"
        # a later source uses the later name for an earlier event
        named = {
            dict(a.qualifiers).get("referred_to_as")
            for a in u.ASSERTIONS
            if a.value == "floerburg"
        }
        assert {"Bad Flörburg", "Floerburg"} <= named

    def test_a_town_in_two_districts(self):
        assert len(u.containment_paths("flaeckfeld")) == 2


class TestEventsAndDates:
    @pytest.mark.parametrize("kind", PERSECUTION_KINDS)
    def test_persecution_events_are_events(self, kind):
        events = [e.key for e in u.ENTITIES if e.kind == kind]
        assert events
        for key in events:
            people = values(key, "participant")
            assert people and all(p in PEOPLE for p in people)
            assert values(key, "point_in_time") or values(key, "start_time")
            for where in values(key, "location"):
                assert where in PLACES, (key, where)

    def test_arrest_imprisonment_and_release_line_up(self):
        (held_from,) = values("imprisonment-1939", "start_time")
        (held_to,) = values("imprisonment-1939", "end_time")
        (released,) = values("release-1939", "point_in_time")
        assert u.Date(1939, 9, 3) in values("arrest-1939", "point_in_time")
        assert held_from == u.Date(1939, 9, 3)
        assert released == held_to
        assert PLACES[values("release-1939", "location")[0]].kind == "prison"

    @pytest.mark.parametrize(("subject", "prop"), u.CONTRADICTIONS)
    def test_contradictions_across_two_sources(self, subject, prop):
        assertions = u.about(subject, prop)
        assert len({a.source for a in assertions}) >= 2

        def disagree(first, second):
            if isinstance(first.value, u.Date):
                return not overlap(first.value, second.value)
            return first.value != second.value

        assert any(
            first.source != second.source and disagree(first, second)
            for first, second in combinations(assertions, 2)
        )

    @pytest.mark.parametrize(("subject", "prop"), u.REFINEMENTS)
    def test_refinements_are_not_contradictions(self, subject, prop):
        assertions = u.about(subject, prop)
        assert len({a.source for a in assertions}) >= 2
        for first, second in combinations(assertions, 2):
            if isinstance(first.value, u.Date):
                assert overlap(first.value, second.value)
            else:
                paths = u.containment_paths(first.value) + u.containment_paths(
                    second.value
                )
                assert any(
                    first.value in path and second.value in path
                    for path in paths
                )

    def test_uncertain_dates_cover_every_form(self):
        found = dates()
        assert {d.qualifier for d in found} >= {
            "circa", "before", "after", "between",
        }  # fmt: skip
        assert {d.precision for d in found} >= {
            "day", "month", "year", "decade",
        }  # fmt: skip

    @pytest.mark.parametrize(
        ("when", "text", "gedcom", "qualifiers"),
        [
            (
                u.Date(1840, qualifier="circa"),
                "c. 1840",
                "ABT 1840",
                {"sourcing_circumstances"},
            ),
            (
                u.Date(1926, qualifier="before"),
                "before 1926",
                "BEF 1926",
                {"latest_date"},
            ),
            (
                u.Date(1945, qualifier="after"),
                "after 1945",
                "AFT 1945",
                {"earliest_date"},
            ),
            (
                u.Date(1937, qualifier="between", until=u.Date(1938)),
                "between 1937 and 1938",
                "BET 1937 AND 1938",
                {"earliest_date", "latest_date"},
            ),
            (
                u.Date(1890, decade=True),
                "1890s",
                "BET 1890 AND 1899",
                {"earliest_date", "latest_date"},
            ),
            (u.Date(1911, 10, 1), "1 October 1911", "1 OCT 1911", set()),
        ],
    )
    def test_date_renderings_keep_the_uncertainty(
        self, when, text, gedcom, qualifiers
    ):
        assert when in dates()
        assert str(when) == text
        assert when.gedcom == gedcom
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
        assert living == ["nicholas-lobig"]
        assert all(
            when.year >= 1940 and when.precision == "year"
            for when in values("nicholas-lobig", "date_of_birth")
        )


@pytest.fixture(scope="module")
def records():
    return parse_gedcom(u.GEDCOM_FIXTURE.read_text())


@pytest.fixture(scope="module")
def by_xref():
    return {p.gedcom_xref: p for p in u.PEOPLE if p.gedcom_xref}


def _gedcom_dates(key: str, prop: str, qualifier: str | None = None):
    """GEDCOM phrases of every date the universe asserts for a person's
    property (or for a qualifier on that property)."""
    found = set()
    for assertion in u.about(key, prop):
        when = (
            dict(assertion.qualifiers).get(qualifier)
            if qualifier
            else assertion.value
        )
        if isinstance(when, u.Date):
            found.add(when.gedcom)
    return found


def _place_names(key: str) -> set[str]:
    place = PLACES[key]
    return {place.label} | {n.text for n in place.names}


class TestGedcomFixture:
    """example-family.ged renders the family; it asserts nothing the
    universe's sources do not."""

    def test_every_individual_is_a_universe_person(self, records, by_xref):
        individuals = [r for r in records if r.tag == "INDI"]
        assert [r.xref for r in individuals] == list(by_xref)
        sexes = {"M": "male", "F": "female", "U": None}
        for record in individuals:
            assert by_xref[record.xref].sex == sexes[record.text("SEX")]

    def test_names_are_universe_name_forms(self, records, by_xref):
        for record in (r for r in records if r.tag == "INDI"):
            person = by_xref[record.xref]
            # the label counts too: the widow is known only as written
            forms = {person.label: None} | {
                n.text: n.form for n in person.names
            }
            for name in record.all("NAME"):
                plain = name.value.replace("/", "")
                nicknames = re.findall(r"\((\w+)\)", plain)
                plain = re.sub(r" \(\w+\)", "", plain)
                assert plain in forms, (record.xref, plain)
                for nick in [*nicknames, name.text("NICK")]:
                    if nick:
                        assert forms[nick] in {"nickname", "diminutive"}
                kind = name.text("TYPE")
                if kind:
                    expected = {"immigrant": "anglicised"}.get(kind, kind)
                    assert forms[plain] == expected, (record.xref, plain)
            for married in record.all("_MARNM"):
                assert forms[married.value.replace("/", "")] == "married"

    @pytest.mark.parametrize(
        ("tag", "prop", "qualifier"),
        [
            ("BIRT", "date_of_birth", None),
            ("DEAT", "date_of_death", None),
            ("BURI", "burial_place", "point_in_time"),
            ("RESI", "residence", "point_in_time"),
        ],
    )
    def test_dates_are_asserted(self, records, by_xref, tag, prop, qualifier):
        for record in (r for r in records if r.tag == "INDI"):
            key = by_xref[record.xref].key
            for event in record.all(tag):
                when = event.text("DATE")
                if when:
                    assert when in _gedcom_dates(key, prop, qualifier), (
                        record.xref,
                        tag,
                        when,
                    )

    def test_places_are_universe_places(self, records):
        known = set().union(*(_place_names(key) for key in PLACES))
        places = [
            line.split(" PLAC ", 1)[1]
            for line in u.GEDCOM_FIXTURE.read_text().splitlines()
            if " PLAC " in line
        ]
        assert places
        for place in places:
            head = place.split(", ")[0]
            assert head in known, place

    def test_families_match_the_assertions(self, records, by_xref):
        facts = {(a.subject, a.property, a.value) for a in u.ASSERTIONS}
        for family in (r for r in records if r.tag == "FAM"):
            husband = by_xref[family.text("HUSB")].key
            wife = by_xref[family.text("WIFE")].key
            assert (husband, "spouse", wife) in facts or (
                wife,
                "spouse",
                husband,
            ) in facts
            marriage = family.first("MARR")
            if marriage:
                starts = _gedcom_dates(
                    husband, "spouse", "start_time"
                ) | _gedcom_dates(wife, "spouse", "start_time")
                assert marriage.text("DATE") in starts
            for child in family.all("CHIL"):
                child_key = by_xref[child.value].key
                assert (child_key, "father", husband) in facts
                assert (child_key, "mother", wife) in facts

    def test_media_are_universe_sources(self, records):
        titles = {source.title: source.key for source in u.SOURCES}
        media = [r for r in records if r.tag == "OBJE"]
        assert media
        for obje in media:
            key = titles[obje.first("FILE").text("TITL")]
            assert obje.text("DATE") in _gedcom_dates(key, "point_in_time")

    def test_the_possibly_living_person_has_no_death(self, records, by_xref):
        (nicholas,) = [
            r
            for r in records
            if r.tag == "INDI" and by_xref[r.xref].key == "nicholas-lobig"
        ]
        assert nicholas.first("DEAT") is None
        assert len(nicholas.first("BIRT").text("DATE")) == 4  # year only
