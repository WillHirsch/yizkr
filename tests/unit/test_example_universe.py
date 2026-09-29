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

# Kinship words in the universe's two languages, to spot a kinship term
# inside an appellation (ADR-0018).
KINSHIP_WORDS = {
    "aunt", "brother", "cousin", "dad", "daughter", "father", "grandfather",
    "grandma", "grandmother", "grandpa", "husband", "mama", "mother", "mum",
    "nana", "nephew", "niece", "papa", "sister", "son", "uncle", "wife",
    "bruder", "großmutter", "großvater", "mutter", "mutti", "neffe",
    "nichte", "oma", "onkel", "opa", "schwester", "sohn", "tante",
    "tochter", "vater", "vati", "vetter",
}  # fmt: skip


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
        # a sourcing circumstance is a vocabulary item: the manifest's, or
        # one it is planned to gain
        circumstances = set(manifest.items) | set(u.PLANNED_VOCABULARY)
        for assertion in u.ASSERTIONS:
            assert assertion.subject in KEYS, assertion
            assert assertion.source in SOURCES, assertion
            for prop, value in (
                (assertion.property, assertion.value),
                *assertion.qualifiers,
            ):
                datatype = manifest.properties[prop].datatype
                if prop == "sourcing_circumstances":
                    assert value in circumstances, (prop, value)
                elif datatype == "wikibase-item":
                    assert value in KEYS, (prop, value)
                elif datatype == "time":
                    assert isinstance(value, u.Date), (prop, value)
                elif datatype == "quantity":
                    assert isinstance(value, int), (prop, value)
                else:
                    assert isinstance(value, str) and value, (prop, value)

    def test_planned_vocabulary_is_not_in_the_manifest_yet(self):
        # an item leaves u.PLANNED_VOCABULARY when the manifest gains it
        gained = set(u.PLANNED_VOCABULARY) & set(load_manifest().items)
        assert not gained, (
            f"the manifest now holds {sorted(gained)}: drop them from "
            "PLANNED_VOCABULARY, and update the sentences that call them "
            "planned: the comment above PLANNED_VOCABULARY in "
            "tests/fixtures/example_universe.py, and docs/EXAMPLES.md, "
            '"Using it": "One value is planned rather than in the manifest '
            'yet"'
        )

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
        } >= {"caecilie-loebig", "hildegard-loebig", "ida-loebig"}

    def test_german_and_english_forms_and_diminutives(self):
        crossed = [
            person.key
            for person in u.PEOPLE
            if any(n.form == "birth" and n.lang == "de" for n in person.names)
            and any(n.form == "anglicised" for n in person.names)
        ]
        assert crossed == ["gustav-loebig", "klara-loebig"]
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
            "ursula-loebig",
            "ursula-loebig-angerring",
        }
        # one is family (parents, a generation), the other is not
        assert values("ursula-loebig", "father") == ["sebastian-loebig"]
        assert PEOPLE["ursula-loebig-angerring"].generation is None
        assert not values("ursula-loebig-angerring", "father")
        # the directory lists the widow when the other Ursula is a baby
        (listed,) = u.about("ursula-loebig-angerring", "residence")
        listed_in = dict(listed.qualifiers)["point_in_time"]
        (born,) = values("ursula-loebig", "date_of_birth")
        assert listed_in.year - born.year == 1

    def test_a_person_without_name_or_sex(self):
        infant = PEOPLE["infant-loebig"]
        assert infant.sex is None
        assert [n.form for n in infant.names] == ["placeholder"]

    def test_a_courtesy_title_is_not_kinship(self):
        # ADR-0018: "Nana Maisie" is an appellation, not a relation
        (called,) = u.about("maisie-cutmore", "referred_to_as")
        assert called.value == "Nana Maisie"
        assert dict(called.qualifiers) == {"perspective": "nicholas-lobig"}
        assert PEOPLE["maisie-cutmore"].generation is None
        kin = {"father", "mother", "child", "sibling", "spouse", "relative"}
        assert not [
            a
            for a in u.ASSERTIONS
            if a.property in kin and "maisie-cutmore" in (a.subject, a.value)
        ]
        # the same source's literal kinship term is a relation, with the
        # aunt as subject and the ego who calls her so as object
        (aunt,) = [
            a
            for a in u.about("klara-loebig", "relative")
            if a.value == "nicholas-lobig"
        ]
        assert aunt.source == called.source
        assert dict(aunt.qualifiers) == {"kinship_type": "aunt"}

    def test_a_kinship_term_is_never_an_appellation(self):
        # ADR-0018 Ruling 2: a literal kinship term is recorded as the
        # kinship (`relative` + `kinship_type`, or an ADR-0014 primitive
        # such as `father`), and `referred_to_as` is kept for appellations
        # that are not kinship.  A kinship word in one makes it a courtesy
        # title: the referent is no relation of the ego.
        edges = {"father", "mother", "child", "sibling", "spouse", "relative"}
        related = {
            frozenset((a.subject, a.value))
            for a in u.ASSERTIONS
            if a.property in edges
        }
        appellations = [
            a for a in u.ASSERTIONS if a.property == "referred_to_as"
        ]
        courtesy = []
        for a in appellations:
            if set(re.findall(r"\w+", a.value.lower())) & KINSHIP_WORDS:
                ego = dict(a.qualifiers).get("perspective")
                assert frozenset((a.subject, ego)) not in related, a
                courtesy.append(a.value)
        assert courtesy == ["Nana Maisie"]


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

    def test_kinship_terms_run_from_the_referent_to_the_ego(self):
        # ADR-0018: `relative` + `kinship_type` has the referent as subject
        # and the ego as object, so the term is what the subject is to the
        # object.  The universe's parent edges bear out each one.
        parents: dict[str, set[str]] = {}
        for a in u.ASSERTIONS:
            if a.property in {"father", "mother"}:
                parents.setdefault(a.subject, set()).add(a.value)

        def children(of: set[str]) -> set[str]:
            return {key for key, theirs in parents.items() if theirs & of}

        def aunts_and_uncles(ego: str) -> set[str]:
            mine = parents.get(ego, set())
            grandparents = set().union(*(parents.get(p, set()) for p in mine))
            return children(grandparents) - mine

        derived = {
            "aunt": lambda ego: {
                key
                for key in aunts_and_uncles(ego)
                if PEOPLE[key].sex == "female"
            },
            "cousin": lambda ego: children(aunts_and_uncles(ego)),
        }
        kinship = [a for a in u.ASSERTIONS if a.property == "relative"]
        assert kinship
        for assertion in kinship:
            term = dict(assertion.qualifiers)["kinship_type"]
            assert assertion.subject in derived[term](assertion.value), (
                assertion
            )
        # a cousin is a cousin both ways, so pin who each term names: the
        # subject is the referent ("Aunt Pünktchen" is Klara, "Dad's cousin
        # Leopold" is Leopold) and the object is the ego
        assert {
            (a.subject, a.value, dict(a.qualifiers)["kinship_type"])
            for a in kinship
        } == {
            ("klara-loebig", "nicholas-lobig", "aunt"),
            ("leopold-loebig", "gustav-loebig", "cousin"),
        }

    def test_spouses_in_three_generations(self):
        couples = {
            frozenset((a.subject, a.value))
            for a in u.ASSERTIONS
            if a.property == "spouse"
        }
        generations = {
            PEOPLE[next(iter(couple))].generation for couple in couples
        }
        assert len(couples) == 5
        assert generations == {1, 2, 3}

    def test_a_divorce_dated_by_a_source(self):
        ended = [
            a
            for a in u.ASSERTIONS
            if a.property == "spouse" and "end_time" in dict(a.qualifiers)
        ]
        assert {(a.subject, a.value) for a in ended} == {
            ("leopold-loebig", "agnes-loebig")
        }
        ends = {a.source: dict(a.qualifiers)["end_time"] for a in ended}
        # the court's judgment gives the day; the tree only the year
        assert ends == {
            "divorce-judgment-1957": u.Date(1957, 1, 2),
            "family-tree-1984": u.Date(1957),
        }
        for assertion in ended:
            quals = dict(assertion.qualifiers)
            married_by = bounds(quals["start_time"])[1]
            assert married_by < bounds(quals["end_time"])[0]
        # both outlived the marriage: a divorce ended it, not a death
        for key in ("leopold-loebig", "agnes-loebig"):
            deaths = values(key, "date_of_death")
            assert deaths and all(when.year > 1957 for when in deaths)

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

    def test_a_place_inside_a_same_named_parent(self):
        nested = [
            (place.key, parent)
            for place in u.PLACES
            for parent in place.part_of
            if PLACES[parent].label == place.label
        ]
        assert nested == [("rekau", "stadtkreis-rekau")]
        assert [PLACES[key].kind for key in nested[0]] == ["town", "district"]
        (path,) = u.containment_paths("rekau")
        assert path[-1] == "germany"
        # a source locates the court in the town, not the district
        assert values("landgericht-rekau", "location") == ["rekau"]


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
                u.Date(1833, qualifier="circa"),
                "c. 1833",
                "ABT 1833",
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
                u.Date(1938, qualifier="between", until=u.Date(1939)),
                "between 1938 and 1939",
                "BET 1938 AND 1939",
                {"earliest_date", "latest_date"},
            ),
            (
                u.Date(1890, decade=True),
                "1890s",
                "BET 1890 AND 1899",
                {"earliest_date", "latest_date"},
            ),
            (u.Date(1909, 3, 16), "16 March 1909", "16 MAR 1909", set()),
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


class TestCompilation:
    """ADR-0008, as amended by #289: an original excerpt that a compilation
    embeds is an artifact of its own, part of the compilation, so the
    original and the compilation's translation of it are two witnesses."""

    MARKED = ("sourcing_circumstances", "machine_assisted")
    # a page of a compilation that says its translations were made with
    # machine assistance
    STATEMENT = re.compile(
        r"^\[page \d+\] [^\n]*\btranslations\b[^\n]*"
        r"\b(?:software|machine|computer|online translator)\b",
        re.MULTILINE | re.IGNORECASE,
    )
    # a compilation's translation of the excerpt beside it, one line each
    TRANSLATION = re.compile(r"^Translation: .*$", re.MULTILINE)

    @staticmethod
    def embedded() -> list[u.Assertion]:
        """Each part-of link from a source to a source that embeds it;
        the universe has at least one."""
        links = [
            a
            for a in u.ASSERTIONS
            if a.property == "part_of"
            and a.subject in SOURCES
            and a.value in SOURCES
        ]
        assert links
        return links

    @staticmethod
    def said_by(key: str) -> list[u.Assertion]:
        return [a for a in u.ASSERTIONS if a.source == key]

    @classmethod
    def claim(cls, a: u.Assertion) -> tuple:
        """What a claim says, the marker aside: its subject, property and
        other qualifiers."""
        return (
            a.subject,
            a.property,
            frozenset(set(a.qualifiers) - {cls.MARKED}),
        )

    def test_an_excerpt_is_part_of_its_compilation(self):
        for link in self.embedded():
            excerpt, compilation = SOURCES[link.subject], SOURCES[link.value]
            # a raster image placed within a page is classed a photograph
            assert excerpt.artifact_class == "photograph"
            # the compilation asserts its own composition, with the page
            assert link.source == compilation.key
            page = dict(link.qualifiers)["page_number"]
            # labelled "<compilation title> — page <n>, image <k>", and the
            # compilation's text marks the image on that page
            label = f"{compilation.title} — page {page}, image "
            placed = re.fullmatch(
                re.escape(label) + r"([1-9]\d*)", excerpt.title
            )
            assert placed, excerpt.title
            assert f"[page {page}] [image {placed[1]}]" in compilation.text
            # the compilation's text is its own: nothing read from the
            # excerpt is in it
            for a in self.said_by(excerpt.key):
                assert a.excerpt and a.excerpt not in compilation.text, a

    def test_the_marker_is_on_the_translation_alone(self):
        for link in self.embedded():
            text = SOURCES[link.value].text
            stated = bool(self.STATEMENT.search(text))
            translation = self.TRANSLATION.findall(text)
            assert translation
            # a claim of the compilation carries the marker exactly when
            # it is read from a translation that the compilation says was
            # made with machine assistance
            for a in self.said_by(link.value):
                translated = bool(a.excerpt) and any(
                    a.excerpt in line for line in translation
                )
                assert (self.MARKED in a.qualifiers) == (
                    stated and translated
                ), a
            # and no claim of the original carries it
            for a in self.said_by(link.subject):
                assert self.MARKED not in a.qualifiers, a

    def test_a_translation_and_its_original_are_two_witnesses(self):
        for link in self.embedded():
            original = self.said_by(link.subject)
            compilation = self.said_by(link.value)
            translated = [
                a for a in compilation if self.MARKED in a.qualifiers
            ]
            assert translated
            # each claim read from the translation renders one that the
            # original makes, with the same qualifiers, and at least one
            # gives it another value
            read: dict[tuple, set] = {}
            for a in original:
                read.setdefault(self.claim(a), set()).add(a.value)
            assert {self.claim(a) for a in translated} <= set(read)
            misread = {
                self.claim(a)[:2]
                for a in translated
                if a.value not in read[self.claim(a)]
            }
            assert misread
            assert misread <= set(u.CONTRADICTIONS)
            # the compilation's own text says something the original does
            # not, with no marker
            own = {
                (a.subject, a.property)
                for a in compilation
                if self.MARKED not in a.qualifiers and a.subject in PEOPLE
            }
            assert own - {claim[:2] for claim in read}


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
        divorces = 0
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
            divorce = family.first("DIV")
            if divorce:
                divorces += 1
                ends = _gedcom_dates(
                    husband, "spouse", "end_time"
                ) | _gedcom_dates(wife, "spouse", "end_time")
                assert divorce.text("DATE") in ends
            for child in family.all("CHIL"):
                child_key = by_xref[child.value].key
                assert (child_key, "father", husband) in facts
                assert (child_key, "mother", wife) in facts
        assert divorces == 1

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
