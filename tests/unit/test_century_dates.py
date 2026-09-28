"""Offline tests for how a century is dated (#241): centuries are counted
as Wikibase and Wikidata count them, the 19th century running from 1801 to
1900, and a claim gives a century by its first year (docs/MODELING.md,
"Uncertainty and time"). test_claims.py pins the claim schema,
test_disagreement.py the report's spans and test_staticsite.py the site's
formatter; this file follows a century from the claim through the writer,
to the value Wikibase stores and the one the query service serves, and on
to what the site shows and the span the report compares. It also pins the
rule the extraction prompts carry, and the refusal of a century given in
another count. tests/integration/test_live_century_dates.py checks the
same on the running stack, Wikibase's own display included.

Two helpers stand in for Wikibase here, each after its source:
MwTimeIsoFormatter names the century of a stored year ceil(|year| / 100),
and DateTimeValueCleaner serves a date of year precision or coarser as 1
January of its year, moving a year before the common era one on, into
XSD 1.1's count where 1 BCE is year 0, only at year precision or finer.
"""

from __future__ import annotations

import json
import math

import pytest

from tests.unit.test_pipeline import (
    ARTIFACT,
    MEDIA_SHA1,
    PROPERTIES,
    RUN,
    FakeWiki,
)
from yizkr import disagreement
from yizkr.claims import ClaimBatch
from yizkr.pipeline.extract import (
    claim_schema_prompt_text,
    salvage_valid_claims,
)
from yizkr.pipeline.writer import BatchWriter
from yizkr.staticsite import format_time

CENTURY, YEAR = 7, 9


def birth(value: dict) -> dict:
    """Eugen Löbig's birth (here) as a claim citing the fictional scan."""
    return {
        "subject": {
            "kind": "local",
            "local_id": "eugen",
            "label": "Eugen Löbig",
            "instance_of": "human",
        },
        "property": "date_of_birth",
        "value": value,
        "source": {"artifact": ARTIFACT},
    }


def written(value: dict) -> dict:
    """The time datavalue the writer stores for a birth claim."""
    wiki = FakeWiki({MEDIA_SHA1: "Q23"})
    result = BatchWriter(wiki).write_batch(
        ClaimBatch.model_validate(
            {"extraction_run": RUN, "claims": [birth(value)]}
        )
    )
    person = wiki.wbi.item.get(result.created_items["eugen"])
    (statement,) = person.claims.get(PROPERTIES["date_of_birth"])
    return statement.mainsnak.datavalue["value"]


def stored_year(stored: dict) -> int:
    sign, digits = stored["time"][0], stored["time"][1:].split("-")[0]
    return -int(digits) if sign == "-" else int(digits)


def wikibase_shows(stored: dict) -> str:
    """What Wikibase's English formatter shows for a stored
    century-precision time: the century ceil(|year| / 100)."""
    assert stored["precision"] == CENTURY
    year = stored_year(stored)
    shown = f"{math.ceil(abs(year) / 100)}. century"
    return f"{shown} BCE" if year < 0 else shown


def served(stored: dict) -> str:
    """The xsd:dateTime the query service gives for a stored time of year
    precision or coarser."""
    year = stored_year(stored)
    if stored["precision"] >= YEAR and year < 0:
        year += 1
    return f"{'-' if year < 0 else ''}{abs(year):04d}-01-01T00:00:00Z"


class TestTheRoundTrip:
    """A century from the claim to what every reader makes of it."""

    @pytest.mark.parametrize(
        ("year", "stored", "wiki", "site", "years"),
        [
            (
                1801,
                "+1801-00-00T00:00:00Z",
                "19. century",
                "19th century",
                (1801, 1900),
            ),
            # a negative year counts back from 1 BCE: the 18th century BCE
            # is 1800 to 1701 BCE, and the report's span is in XSD 1.1's
            # count, in which a year before the common era is served
            (
                -1800,
                "-1800-00-00T00:00:00Z",
                "18. century BCE",
                "−18th century",
                (-1799, -1700),
            ),
        ],
        ids=["19th century", "18th century BCE"],
    )
    def test_a_century_reads_alike_everywhere(
        self, year, stored, wiki, site, years
    ):
        value = written({"type": "time", "year": year, "precision": "century"})
        assert (value["time"], value["precision"]) == (stored, CENTURY)
        assert wikibase_shows(value) == wiki
        time = served(value)
        assert format_time(time, str(CENTURY)) == site
        first, last = years
        assert disagreement.period(disagreement.Time(time, CENTURY)) == (
            first * 10000,
            last * 10000 + 9999,
        )

    @pytest.mark.parametrize(
        ("year", "inside"), [(1801, True), (1900, True), (1800, False)]
    )
    def test_the_report_holds_the_years_the_wiki_counts_in_it(
        self, year, inside
    ):
        # the 19th century and a year beside it, each written, served and
        # compared: its first and last years lie inside it, 1800 does not
        def compared(value: dict) -> disagreement.Span:
            stored = written(value)
            return disagreement.period(
                disagreement.Time(served(stored), stored["precision"])
            )

        century = compared(
            {"type": "time", "year": 1801, "precision": "century"}
        )
        dated = compared({"type": "time", "year": year})
        assert disagreement.spans_nest(century, dated) is inside


class TestTheExtractionPrompts:
    """The extractor is told the count, in the schema both prompts carry."""

    def test_the_schema_gives_a_century_by_its_first_year(self):
        # the TimeValue docstring is the schema's description, which both
        # prompts embed as claim_schema_prompt_text
        schema = json.loads(claim_schema_prompt_text())
        description = " ".join(
            schema["$defs"]["TimeValue"]["description"].split()
        )
        assert (
            "Centuries are counted as Wikibase counts them: the 19th century "
            "is 1801-1900, so use its first year (19th century -> "
            "year=1801, precision=century); 1800 is in the 18th."
        ) in description
        # hundreds named by their digits are a range, never rounded to the
        # century Wikibase counts
        assert (
            "The 1800s, meaning 1800-1899, are no such century: give them as "
            "a range."
        ) in description

    def test_a_century_given_in_another_count_is_refused(self):
        # the 19th century given as 1800 names the 18th as Wikibase counts:
        # the claim is refused and reported, never written a century off
        batch, rejects = salvage_valid_claims(
            [birth({"type": "time", "year": 1800, "precision": "century"})],
            RUN,
        )
        assert batch.claims == []
        (reject,) = rejects
        assert "is not a century's first year" in reject["problems"][0]
