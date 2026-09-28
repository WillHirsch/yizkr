"""Offline tests for how a period is dated (#231): a marriage's date is its
spouse statement's start time and its end the end time, a point in time on
a spouse or residence statement dates an observation, and each end has a
range of its own (docs/MODELING.md, "Marriages, residences and other
periods"). test_gedcom.py pins the GEDCOM adapter and test_disagreement.py
the disagreement report; this file pins the extraction prompts, the
manifest's bounds and the person timeline.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.test_queries import VALUES_BODY, group_at
from yizkr import disagreement
from yizkr.claims import ClaimBatch
from yizkr.model.schema import load_manifest
from yizkr.pipeline.chunking import CHUNK_PROMPT
from yizkr.pipeline.extract import EXTRACTION_PROMPT, vocabulary_prompt_lines
from yizkr.queries.publish import load_templates

PROMPTS = pytest.mark.parametrize(
    "prompt", [CHUNK_PROMPT, EXTRACTION_PROMPT], ids=["chunked", "single"]
)


def flat(text: str) -> str:
    return " ".join(text.split())


def dating_rule(prompt: str) -> str:
    """The prompt's bullet on dating a period."""
    (rule,) = re.findall(
        r"^- Dating a marriage.*?(?=^- )", prompt, re.M | re.S
    )
    return flat(rule)


class TestTheExtractionPrompts:
    """The extractor is told the convention, in both prompts alike."""

    @PROMPTS
    def test_a_marriage_is_dated_by_its_start_and_end(self, prompt):
        rule = dating_rule(prompt)
        assert (
            "a marriage's date is `start_time` on the `spouse` claim" in rule
        )
        assert "(a divorce, an annulment, widowhood) is its `end_time`" in rule
        assert (
            "a move in and a move out date a `residence` the same way" in rule
        )

    @PROMPTS
    def test_a_point_in_time_is_an_observation_never_a_wedding(self, prompt):
        rule = dating_rule(prompt)
        assert (
            "`point_in_time` on a spouse or residence claim only records a "
            "date at which the source shows the marriage or the residence "
            "holding"
        ) in rule
        assert "never a wedding date" in rule

    @PROMPTS
    def test_a_range_bounds_one_end(self, prompt):
        rule = dating_rule(prompt)
        assert (
            "a start takes `earliest_date` and `latest_start_date`, an end "
            "takes `earliest_end_date` and `latest_date`"
        ) in rule
        assert (
            'Married "between X and Y" is start_time X with earliest_date X '
            "and latest_start_date Y"
        ) in rule
        assert 'divorced "before Z" is end_time Z with latest_date Z' in rule

    def test_both_prompts_give_the_same_rule(self):
        assert dating_rule(CHUNK_PROMPT) == dating_rule(EXTRACTION_PROMPT)

    def test_the_property_list_says_which_end_each_bound_dates(self):
        properties, _ = vocabulary_prompt_lines()
        lines = {
            line.split(":", 1)[0].strip(): line
            for line in properties.splitlines()
        }
        assert "the earliest its start can be" in lines["earliest_date"]
        assert "the latest its start can be" in lines["latest_start_date"]
        assert "the earliest its end can be" in lines["earliest_end_date"]
        assert "the latest its end can be" in lines["latest_date"]

    def test_the_schema_names_the_bounds_of_each_end(self):
        # the TimeValue docstring is the schema's description, which the
        # prompts embed
        schema = ClaimBatch.model_json_schema()
        description = flat(schema["$defs"]["TimeValue"]["description"])
        assert (
            "earliest_date/latest_start_date for a start, "
            "earliest_end_date/ latest_date for an end"
        ) in description


class TestTheManifest:
    def test_each_end_has_its_wikidata_bounds(self):
        # Wikidata's practice on spouse (P26): earliest date (P1319) and
        # latest start date (P8555) bound a start; earliest end date
        # (P8554) and latest date (P1326) an end
        properties = load_manifest().properties
        for key, wikidata in (
            ("earliest_date", "P1319"),
            ("latest_start_date", "P8555"),
            ("earliest_end_date", "P8554"),
            ("latest_date", "P1326"),
        ):
            assert properties[key].datatype == "time", key
            assert properties[key].wikidata == wikidata, key

    def test_the_report_bounds_each_end_with_them(self):
        assert disagreement.BOUNDS == {
            "": ("earliest_date", "latest_date"),
            "start_time": ("earliest_date", "latest_start_date"),
            "end_time": ("earliest_end_date", "latest_date"),
        }


TOKEN = re.compile(r'"[^"]*"@\w+|\S+')


def values_rows(template: str, *variables: str) -> list[tuple[str, ...]]:
    """The rows of every ``VALUES (?a ?b ...) { ... }`` block over exactly
    these variables, each placeholder as its manifest key (``pq:`` kept)."""
    head = r"\s+".join(rf"\?{name}" for name in variables)
    rows = []
    for body in re.findall(
        rf"VALUES\s*\(\s*{head}\s*\)\s*" + VALUES_BODY, template
    ):
        for row in re.findall(r"\(([^()]*)\)", body):
            rows.append(
                tuple(
                    re.sub(r"^(pq:)?(?:wd:)?\{P:(\w+)\}$", r"\1\2", token)
                    for token in TOKEN.findall(row)
                )
            )
    return rows


class TestThePersonTimeline:
    """person-timeline.rq lists a marriage by its spouse statement's start
    and end time, a point in time as an observation, and each date with the
    range its source states for it. Live, the query service runs it
    (tests/integration/test_live_queries.py)."""

    @pytest.fixture
    def q(self) -> str:
        return load_templates()["person-timeline"]

    def test_each_date_on_a_statement_takes_its_own_bounds(self, q):
        rows = values_rows(q, "qualifier", "dated", "low", "high")
        assert rows == [
            (
                "pq:start_time",
                "start_time",
                "earliest_date",
                "latest_start_date",
            ),
            ("pq:end_time", "end_time", "earliest_end_date", "latest_date"),
            (
                "pq:point_in_time",
                "point_in_time",
                "earliest_date",
                "latest_date",
            ),
        ]
        # the ends are bounded as the disagreement report bounds them
        for _, dated, low, high in rows[:2]:
            assert disagreement.BOUNDS[dated] == (low, high)

    def test_a_date_value_takes_its_earliest_and_latest_date(self, q):
        births_and_deaths = values_rows(q, "what", "dated", "low", "high")
        assert births_and_deaths == [
            (
                '"date of birth"@en',
                "date_of_birth",
                "earliest_date",
                "latest_date",
            ),
            (
                '"date of death"@en',
                "date_of_death",
                "earliest_date",
                "latest_date",
            ),
        ]
        # an event's own point in time
        assert values_rows(q, "dated", "low", "high") == [
            ("point_in_time", "earliest_date", "latest_date")
        ]

    def test_a_point_in_time_beside_a_start_or_end_takes_no_range(self, q):
        ranged = q[q.index("BIND(?dated") : q.index("AS ?ranged)")]
        assert "?dated != wd:{P:point_in_time}" in ranged
        assert (
            "NOT EXISTS { ?st pq:{P:start_time}|pq:{P:end_time} [] }" in ranged
        )
        for bound in ("low", "high"):
            lookup = group_at(q, f"?st ?{bound}Link ?{bound}Node")
            assert f"?{bound} wikibase:qualifierValue ?{bound}Link" in lookup
            assert "FILTER(?ranged)" in lookup

    def test_a_row_names_the_qualifier_and_the_value(self, q):
        # "spouse: Agnes Löbig (start time)", never a bare "spouse"
        assert "?dated rdfs:label ?qualifierName" in q
        assert (
            'CONCAT(?propertyName, ": ", ?shown,\n'
            '                         " (", ?qualifierName, ")")'
        ) in q
        assert "?claimProperty wikibase:claim ?p ;" in q

    def test_a_date_keeps_its_stated_precision(self, q):
        # the date's own value node, never another node that happens to
        # hold the same time (a range bound at another precision)
        assert "?st ?dateLink ?dateNode ." in q
        assert "?dateNode wikibase:timeValue ?date ;" in q
        assert "wikibase:timePrecision ?precision ." in q
        assert "?anyValuePredicate" not in q

    def test_the_projection_carries_each_range(self, q):
        projection = set(re.findall(r"\?(\w+)", q.split("WHERE", 1)[0]))
        assert {
            "what",
            "date",
            "precision",
            "earliest",
            "earliestPrecision",
            "latest",
            "latestPrecision",
            "source",
        } <= projection
