"""The shared "died by" derivation and a union's derived end (ADR-0040
§2.4, §2.6).

The universe's own death statements, union ends, widowhoods and document
dates feed it, and it gives exactly the universe's DERIVED_DIED_BY.
Every example comes from the fictional universe (docs/EXAMPLES.md); the
two widowhoods in one source and "der selige Felix" are hypothetical, as
ADR-0040 §8 gives them.
"""

from __future__ import annotations

import math

import pytest

from tests.fixtures import example_universe as u
from yizkr import died_by as d
from yizkr.claims import ClaimBatch


def when(date: u.Date) -> d.When:
    return d.When(
        date.year,
        date.month,
        date.day,
        "decade" if date.decade else date.precision,
    )


def dating(date: u.Date) -> d.Dating:
    """A universe date as the derivation reads a statement's date."""
    value = when(date)
    if date.qualifier == "before":
        return d.Dating(value, latest=value)
    if date.qualifier == "after":
        return d.Dating(value, earliest=value)
    if date.qualifier == "between":
        return d.Dating(value, earliest=value, latest=when(date.until))
    return d.Dating(value, circa=date.qualifier == "circa")


def universe_inputs() -> d.Inputs:
    """The derivation's inputs from the universe's assertions."""
    deaths, ends, widowhoods = [], [], []
    inceptions: dict[str, list[d.Dating]] = {}
    for a in u.ASSERTIONS:
        source = frozenset({a.source})
        if a.property == "date_of_death":
            deaths.append(
                d.DeathStatement(
                    a.subject,
                    None if a.value is u.UNKNOWN else dating(a.value),
                    source,
                )
            )
        elif a.property == "ended_by_death_of":
            partners = frozenset(
                s.value
                for s in u.about(a.subject, "participant")
                if s.source == a.source
            )
            ends.append(d.UnionEnd(a.subject, a.value, partners, source))
        elif a.property == "civil_status" and a.value == "widowed":
            point = dict(a.qualifiers).get("point_in_time")
            widowhoods.append(
                d.Widowhood(
                    a.subject,
                    dating(point) if point is not None else None,
                    source,
                )
            )
        elif a.property == "inception" and a.subject == a.source:
            inceptions.setdefault(a.source, []).append(dating(a.value))
    return d.Inputs(
        tuple(deaths),
        tuple(ends),
        tuple(widowhoods),
        {doc: tuple(found) for doc, found in inceptions.items()},
    )


def day(year: int, month: int, day_: int) -> d.When:
    return d.When(year, month, day_, "day")


class TestTheUniverse:
    def test_it_gives_exactly_the_universes_derived_dates(self):
        derived = {
            (b.person, b.by, b.source, b.via)
            for b in d.died_by(universe_inputs())
        }
        assert derived == {
            (b.person, when(b.by), b.source, b.via) for b in u.DERIVED_DIED_BY
        }

    def test_caecilie_by_16_march_1909(self):
        # "Witwer der Cäcilie": at Eugen's widowhood on his death
        (bound,) = [
            b
            for b in d.died_by(universe_inputs())
            if b.source == "burial-register-1909"
        ]
        assert bound == d.DiedBy(
            "caecilie-loebig",
            day(1909, 3, 16),
            "burial-register-1909",
            d.UNION_END,
        )

    def test_rudolf_by_25_december_1958(self):
        # one phrase that is both markers: at Nina's widowhood on the
        # letter's date, beside the certificate's 6 March 1941
        inputs = universe_inputs()
        (bound,) = [
            b for b in d.died_by(inputs) if b.person == "rudolf-loebig"
        ]
        assert (bound.by, bound.source, bound.via) == (
            day(1958, 12, 25),
            "letter-to-leopold-1958",
            d.UNION_END,
        )
        span = d.death_span("rudolf-loebig", inputs.deaths, [bound])
        # the hull of the 1941 dates and the bound open at its early end
        assert span == (-math.inf, 19581225)


class TestTheRule:
    LETTER = "postcard-1938"

    def late_felix(self, *inceptions: d.Dating) -> d.Inputs:
        # a hypothetical dated postcard's "der selige Felix"
        return d.Inputs(
            deaths=(
                d.DeathStatement("felix", None, frozenset({self.LETTER})),
            ),
            inceptions={self.LETTER: inceptions},
        )

    def test_an_undated_the_late_gives_no_bound(self):
        assert d.died_by(self.late_felix()) == []

    def test_a_dated_document_bounds_it(self):
        (bound,) = d.died_by(self.late_felix(d.Dating(day(1938, 8, 19))))
        assert (bound.person, bound.by, bound.via) == (
            "felix",
            day(1938, 8, 19),
            d.UNDATED_DEATH,
        )

    def test_never_narrower_than_the_date_it_comes_from(self):
        # "before 19 August 1938": the latest date it allows
        before = d.Dating(day(1938, 8, 19), latest=day(1938, 8, 19))
        (bound,) = d.died_by(self.late_felix(before))
        assert bound.by == day(1938, 8, 19)
        # a document dated only "1890s" gives "by the 1890s"
        decade = d.When(1890, precision="decade")
        (bound,) = d.died_by(self.late_felix(d.Dating(decade)))
        assert bound.by == decade
        # its period's last day, as yyyymmdd numbers read it
        assert bound.span() == (-math.inf, 18999999)
        # the latest of every inception the document has
        (bound,) = d.died_by(
            self.late_felix(
                d.Dating(day(1938, 8, 19)),
                d.Dating(d.When(1938, precision="year")),
            )
        )
        assert bound.by == d.When(1938, precision="year")

    @pytest.mark.parametrize(
        "open_",
        [
            # "after 1945"
            d.Dating(d.When(1945, precision="year"), earliest=d.When(1945)),
            d.Dating(d.When(1938, precision="year"), circa=True),
        ],
    )
    def test_an_open_date_gives_none(self, open_):
        assert d.died_by(self.late_felix(open_)) == []
        assert (
            d.died_by(self.late_felix(d.Dating(day(1938, 8, 19)), open_)) == []
        )

    def test_a_deprecated_statement_gives_none(self):
        inputs = d.Inputs(
            deaths=(
                d.DeathStatement(
                    "felix", None, frozenset({self.LETTER}), deprecated=True
                ),
            ),
            inceptions={self.LETTER: (d.Dating(day(1938, 8, 19)),)},
        )
        assert d.died_by(inputs) == []

    def test_a_dated_death_gives_none(self):
        inputs = d.Inputs(
            deaths=(
                d.DeathStatement(
                    "felix",
                    d.Dating(d.When(1931, precision="year")),
                    frozenset({self.LETTER}),
                ),
            ),
            inceptions={self.LETTER: (d.Dating(day(1938, 8, 19)),)},
        )
        assert d.died_by(inputs) == []


class TestTwoWidowhoodsInOneSource:
    def test_each_late_wife_is_bounded_by_the_latest(self):
        # a hypothetical batch presenting Eugen as widowed on 15 December
        # 1905 and on 16 March 1909, with two unions each ended by a
        # wife's death: the batch cannot tell which widowhood each belongs
        # to, so each wife is bounded by the latest (rule 5)
        source = frozenset({"register"})
        inputs = d.Inputs(
            ends=(
                d.UnionEnd(
                    "m1", "wife-1", frozenset({"eugen", "wife-1"}), source
                ),
                d.UnionEnd(
                    "m2", "wife-2", frozenset({"eugen", "wife-2"}), source
                ),
            ),
            widowhoods=(
                d.Widowhood("eugen", d.Dating(day(1905, 12, 15)), source),
                d.Widowhood("eugen", d.Dating(day(1909, 3, 16)), source),
            ),
        )
        assert [(b.person, b.by) for b in d.died_by(inputs)] == [
            ("wife-1", day(1909, 3, 16)),
            ("wife-2", day(1909, 3, 16)),
        ]

    def test_another_sources_widowhood_counts_for_nothing(self):
        inputs = d.Inputs(
            ends=(
                d.UnionEnd(
                    "m1",
                    "caecilie",
                    frozenset({"eugen", "caecilie"}),
                    frozenset({"register"}),
                ),
            ),
            widowhoods=(
                d.Widowhood(
                    "eugen",
                    d.Dating(day(1909, 3, 16)),
                    frozenset({"another"}),
                ),
            ),
            inceptions={"register": (d.Dating(day(1902, 12, 14)),)},
        )
        (bound,) = d.died_by(inputs)
        assert bound.by == day(1902, 12, 14)

    def test_an_undated_widowhood_falls_back_to_the_documents_date(self):
        source = frozenset({"register"})
        inputs = d.Inputs(
            ends=(
                d.UnionEnd(
                    "m1", "caecilie", frozenset({"eugen", "caecilie"}), source
                ),
            ),
            widowhoods=(d.Widowhood("eugen", None, source),),
            inceptions={"register": (d.Dating(day(1909, 3, 20)),)},
        )
        (bound,) = d.died_by(inputs)
        assert bound.by == day(1909, 3, 20)


class TestADerivedEnd:
    @staticmethod
    def dated(person: str, year: int) -> d.DeathStatement:
        return d.DeathStatement(
            person, d.Dating(d.When(year, precision="year")), frozenset({"s"})
        )

    def test_spans_apart_give_the_first_death(self):
        # Felix died in 1931, Ida in 1946: the union ended by his death
        end = d.derived_end(
            ["felix", "ida"],
            [self.dated("felix", 1931), self.dated("ida", 1946)],
            [],
            [],
        )
        assert end == d.DerivedEnd(by_death_of="felix")

    def test_overlapping_spans_give_only_no_later_than(self):
        deaths = [
            self.dated("felix", 1931),
            d.DeathStatement(
                "ida",
                d.Dating(d.When(1931, 6, precision="month")),
                frozenset({"s"}),
            ),
        ]
        end = d.derived_end(["felix", "ida"], deaths, [], [])
        assert end == d.DerivedEnd(no_later_than=("felix", "ida"))

    def test_a_derived_bound_is_open_at_its_early_end(self):
        # Rudolf died by 25 December 1958 (derived): a death of Nina's in
        # 1990 lies wholly after it, one in 1950 may lie before it
        bound = d.DiedBy("rudolf", day(1958, 12, 25), "letter", d.UNION_END)
        later = d.derived_end(
            ["nina", "rudolf"], [self.dated("nina", 1990)], [], [bound]
        )
        assert later == d.DerivedEnd(by_death_of="rudolf")
        overlapping = d.derived_end(
            ["nina", "rudolf"], [self.dated("nina", 1950)], [], [bound]
        )
        assert overlapping == d.DerivedEnd(no_later_than=("nina", "rudolf"))

    def test_only_one_dated_death(self):
        end = d.derived_end(
            ["felix", "ida"], [self.dated("felix", 1931)], [], []
        )
        assert end == d.DerivedEnd(no_later_than=("felix",))

    def test_an_unknown_value_is_evidence_without_a_span(self):
        unknown = d.DeathStatement("ida", None, frozenset({"s"}))
        end = d.derived_end(
            ["felix", "ida"], [self.dated("felix", 1931), unknown], [], []
        )
        assert end == d.DerivedEnd(no_later_than=("felix", "ida"))

    def test_no_evidence_gives_none(self):
        assert d.derived_end(["felix", "ida"], [], [], []) is None


class TestFromABatch:
    def test_the_burial_registers_claims(self):
        run = {
            "model_identifier": "fake",
            "pipeline_version": "0.1",
            "started_at": "2026-10-24T10:00:00+00:00",
        }
        sha1 = "b" * 40
        source = {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "transcription_excerpt": {
                "text": "Witwer der Cäcilie Löbig geb. Tarle",
                "language": "de",
            },
        }

        def local(local_id: str) -> dict:
            return {"kind": "local", "local_id": local_id}

        def item(ref: dict) -> dict:
            return {"type": "item", "value": ref}

        batch = ClaimBatch.model_validate(
            {
                "extraction_run": run,
                "claims": [
                    {
                        "subject": local("m"),
                        "property": "participant",
                        "value": item(local(person)),
                        "source": source,
                    }
                    for person in ("eugen", "caecilie")
                ]
                + [
                    {
                        "subject": local("m"),
                        "property": "ended_by_death_of",
                        "value": item(local("caecilie")),
                        "source": source,
                    },
                    {
                        "subject": local("eugen"),
                        "property": "civil_status",
                        "value": item({"kind": "vocab", "key": "widowed"}),
                        "qualifiers": [
                            {
                                "property": "point_in_time",
                                "value": {
                                    "type": "time",
                                    "year": 1909,
                                    "month": 3,
                                    "day": 16,
                                },
                            }
                        ],
                        "source": source,
                    },
                    {
                        "subject": {"kind": "artifact", "sha1": sha1},
                        "property": "inception",
                        "value": {"type": "time", "year": 1909},
                        "source": source,
                    },
                ],
            }
        )
        (bound,) = d.died_by(d.from_batch(batch))
        assert bound == d.DiedBy(
            "caecilie", day(1909, 3, 16), sha1, d.UNION_END
        )


class TestFromTheGraph:
    E = "http://example.org/entity/"

    def uri(self, qid: str) -> dict:
        return {"type": "uri", "value": self.E + qid}

    def row(self, kind: str, subject: str, statement: str, **cells) -> dict:
        row = {
            "kind": {"type": "literal", "value": kind},
            "subject": self.uri(subject),
            "statement": {"type": "uri", "value": self.E + statement},
            "rank": {
                "type": "uri",
                "value": "http://wikiba.se/ontology#NormalRank",
            },
            "artifact": self.uri("Q90"),
        }
        for name, value in cells.items():
            row[name] = (
                value
                if isinstance(value, dict)
                else {"type": "literal", "value": value}
            )
        return row

    def test_an_unknown_value_bounded_by_its_documents_date(self):
        rows = [
            self.row(
                "death",
                "Q7",
                "s1",
                value={
                    "type": "uri",
                    "value": "http://example.org/.well-known/genid/ab12",
                },
            ),
            self.row(
                "inception",
                "Q90",
                "s2",
                time="1938-08-19T00:00:00Z",
                timePrecision="11",
            ),
        ]
        (bound,) = d.died_by(d.from_rows(rows))
        assert (bound.person, bound.by, bound.source) == (
            self.E + "Q7",
            day(1938, 8, 19),
            self.E + "Q90",
        )

    def test_a_union_end_with_its_partners(self):
        rows = [
            self.row(
                "end", "Q5", "s1", value=self.uri("Q3"), partner=self.uri(p)
            )
            for p in ("Q2", "Q3")
        ] + [
            self.row(
                "widowed",
                "Q2",
                "s3",
                pointInTime="1909-03-16T00:00:00Z",
                pointInTimePrecision="11",
            )
        ]
        inputs = d.from_rows(rows)
        (end,) = inputs.ends
        assert end.partners == {self.E + "Q2", self.E + "Q3"}
        (bound,) = d.died_by(inputs)
        assert (bound.person, bound.by) == (self.E + "Q3", day(1909, 3, 16))

    def test_a_deprecated_inception_is_not_the_documents_date(self):
        deprecated = self.row(
            "inception",
            "Q90",
            "s2",
            time="1938-08-19T00:00:00Z",
            timePrecision="11",
        )
        deprecated["rank"]["value"] = d.DEPRECATED
        rows = [
            self.row(
                "death", "Q7", "s1", value={"type": "bnode", "value": "b1"}
            ),
            deprecated,
        ]
        assert d.died_by(d.from_rows(rows)) == []


class TestMixedPrecisions:
    def test_bounds_in_one_year_at_different_precisions_sort(self):
        # a document dated 1938 and one dated 19 August 1938 both bound
        # "der selige Felix" (hypothetical): every reader sorts them
        inputs = d.Inputs(
            deaths=(d.DeathStatement("felix", None, frozenset({"a", "b"})),),
            inceptions={
                "a": (d.Dating(d.When(1938, precision="year")),),
                "b": (d.Dating(day(1938, 8, 19)),),
            },
        )
        found = d.died_by(inputs)
        assert [b.by for b in found] == [
            d.When(1938, precision="year"),
            day(1938, 8, 19),
        ]
        assert sorted([day(1938, 8, 19), d.When(1938, 8, precision="month")])


class TestEveryQualifierValue:
    """A statement with several values of one date qualifier keeps them
    all, never narrower than any, whatever order they come in (ADR-0040
    §2.4): the hypothetical batch presenting Eugen as widowed on 15
    December 1905 and on 16 March 1909."""

    RUN = {
        "model_identifier": "fake",
        "pipeline_version": "0.1",
        "started_at": "2026-10-24T10:00:00+00:00",
    }
    SHA1 = "b" * 40

    @staticmethod
    def stamp(year, month, day_):
        return {"type": "time", "year": year, "month": month, "day": day_}

    def batch(self, qualifiers, prop="civil_status") -> ClaimBatch:
        source = {
            "artifact": {"kind": "artifact", "sha1": self.SHA1},
            "transcription_excerpt": {"text": "Witwer", "language": "de"},
        }

        def local(local_id):
            return {"kind": "local", "local_id": local_id}

        claims = [
            {
                "subject": local("m"),
                "property": "participant",
                "value": {"type": "item", "value": local(person)},
                "source": source,
            }
            for person in ("eugen", "caecilie")
        ] + [
            {
                "subject": local("m"),
                "property": "ended_by_death_of",
                "value": {"type": "item", "value": local("caecilie")},
                "source": source,
            },
            {
                "subject": local("eugen"),
                "property": "civil_status",
                "value": {
                    "type": "item",
                    "value": {"kind": "vocab", "key": "widowed"},
                },
                "qualifiers": [
                    {"property": key, "value": value}
                    for key, value in qualifiers
                ],
                "source": source,
            },
        ]
        return ClaimBatch.model_validate(
            {"extraction_run": self.RUN, "claims": claims}
        )

    @pytest.mark.parametrize("reverse", [False, True])
    def test_a_batch_keeps_every_point_in_time(self, reverse):
        points = [
            ("point_in_time", self.stamp(1905, 12, 15)),
            ("point_in_time", self.stamp(1909, 3, 16)),
        ]
        if reverse:
            points.reverse()
        (bound,) = d.died_by(d.from_batch(self.batch(points)))
        assert bound.by == day(1909, 3, 16)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_a_batch_keeps_every_latest_date(self, reverse):
        # a death "before 15 December 1905" and "before 16 March 1909" in
        # one claim: its bound is the later, never the narrower
        qualifiers = [
            ("latest_date", self.stamp(1905, 12, 15)),
            ("latest_date", self.stamp(1909, 3, 16)),
        ]
        if reverse:
            qualifiers.reverse()
        claim = (
            self.batch([])
            .claims[0]
            .model_copy(update={"property": "date_of_death"})
        )
        dated = ClaimBatch.model_validate(
            {
                "extraction_run": self.RUN,
                "claims": [
                    {
                        **claim.model_dump(mode="json", exclude_none=True),
                        "subject": {"kind": "local", "local_id": "eugen"},
                        "value": self.stamp(1904, 1, 1),
                        "qualifiers": [
                            {"property": k, "value": v} for k, v in qualifiers
                        ],
                    }
                ],
            }
        )
        (death,) = d.from_batch(dated).deaths
        assert death.dating.bound() == day(1909, 3, 16)
        assert death.dating.span()[1] == day(1909, 3, 16).period()[1]

    def rows(self, kind, column, values, reverse):
        E = TestFromTheGraph.E
        found = [
            {
                "kind": {"type": "literal", "value": kind},
                "subject": {"type": "uri", "value": E + "Q2"},
                "statement": {"type": "uri", "value": E + "s3"},
                "rank": {
                    "type": "uri",
                    "value": "http://wikiba.se/ontology#NormalRank",
                },
                "artifact": {"type": "uri", "value": E + "Q90"},
                **{
                    name: {"type": "literal", "value": cell}
                    for name, cell in cells.items()
                },
            }
            for cells in values
        ]
        return list(reversed(found)) if reverse else found

    @pytest.mark.parametrize("reverse", [False, True])
    def test_the_graph_keeps_every_point_in_time(self, reverse):
        E = TestFromTheGraph.E
        ends = [
            TestFromTheGraph().row(
                "end",
                "Q5",
                "s1",
                value={"type": "uri", "value": E + "Q3"},
                partner={"type": "uri", "value": E + p},
            )
            for p in ("Q2", "Q3")
        ]
        widowed = self.rows(
            "widowed",
            "pointInTime",
            [
                {
                    "pointInTime": "1905-12-15T00:00:00Z",
                    "pointInTimePrecision": "11",
                },
                {
                    "pointInTime": "1909-03-16T00:00:00Z",
                    "pointInTimePrecision": "11",
                },
            ],
            reverse,
        )
        (bound,) = d.died_by(d.from_rows([*ends, *widowed]))
        assert bound.by == day(1909, 3, 16)

    @pytest.mark.parametrize("reverse", [False, True])
    def test_the_graph_keeps_every_latest_date(self, reverse):
        rows = self.rows(
            "death",
            "time",
            [
                {
                    "time": "1904-01-01T00:00:00Z",
                    "timePrecision": "9",
                    "latest": stamp,
                    "latestPrecision": "11",
                }
                for stamp in ("1905-12-15T00:00:00Z", "1909-03-16T00:00:00Z")
            ],
            reverse,
        )
        (death,) = d.from_rows(rows).deaths
        assert death.dating.bound() == day(1909, 3, 16)


class TestEdges:
    def test_touching_spans_give_only_no_later_than(self):
        # Eugen's death in 1909 and Cäcilie's on 31 December 1909 (both
        # hypothetical): the spans meet on that day, so neither lies
        # wholly before the other; on 1 January 1910, Eugen's does
        deaths = (
            d.DeathStatement(
                "eugen", d.Dating(d.When(1909, precision="year")), frozenset()
            ),
            d.DeathStatement(
                "caecilie",
                d.Dating(d.When(1909, 12, 31, "day")),
                frozenset(),
            ),
        )
        found = d.derived_end(["eugen", "caecilie"], deaths, (), ())
        assert found == d.DerivedEnd(no_later_than=("caecilie", "eugen"))
        apart = (
            deaths[0],
            d.DeathStatement(
                "caecilie", d.Dating(day(1910, 1, 1)), frozenset()
            ),
        )
        assert d.derived_end(
            ["eugen", "caecilie"], apart, (), ()
        ) == d.DerivedEnd(by_death_of="eugen")

    def test_a_burial_alone_gives_no_bound(self):
        # Q3: the register's "Beerdigt: 20. März 1909", with no death
        # statement, gives no "died by" and no death span
        batch = ClaimBatch.model_validate(
            {
                "extraction_run": TestEveryQualifierValue.RUN,
                "claims": [
                    {
                        "subject": {"kind": "local", "local_id": "eugen"},
                        "property": "burial_place",
                        "value": {"type": "unknown"},
                        "qualifiers": [
                            {
                                "property": "point_in_time",
                                "value": {
                                    "type": "time",
                                    "year": 1909,
                                    "month": 3,
                                    "day": 20,
                                },
                            }
                        ],
                        "source": {
                            "artifact": {"kind": "artifact", "sha1": "c" * 40},
                            "transcription_excerpt": {
                                "text": "Beerdigt: 20. März 1909.",
                                "language": "de",
                            },
                        },
                    },
                    {
                        "subject": {"kind": "artifact", "sha1": "c" * 40},
                        "property": "inception",
                        "value": {"type": "time", "year": 1909},
                        "source": {
                            "artifact": {"kind": "artifact", "sha1": "c" * 40}
                        },
                    },
                ],
            }
        )
        inputs = d.from_batch(batch)
        assert inputs.deaths == ()
        assert d.died_by(inputs) == []
        assert d.death_span("eugen", inputs.deaths, ()) is None
