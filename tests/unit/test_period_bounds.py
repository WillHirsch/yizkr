"""Validation of the bounds that date a period's ends (docs/LIFE_EVENTS.md,
"Dates and bounds"): `latest_start_date` and `earliest_end_date` bound a
period's `start_time` and `end_time` qualifiers, so a claim that carries
one with neither qualifier is refused under its own reason code, while
an entity's own start or end takes `earliest_date` and `latest_date`.

Every example comes from the fictional universe (docs/EXAMPLES.md): the
1945 registration card's imprisonment of Leopold Löbig ("arrested
3.9.1939, held in Peiltal court prison until 1.10.1939"), Gustav Löbig's
date of birth and his residence in Australia from 1939, and Felix Löbig's
residence at Wasserallee 59.
"""

from __future__ import annotations

import pytest

from tests.fixtures.example_universe import Entity, Person, Place, by_key
from yizkr.claims import (
    PERIOD_BOUND,
    ClaimBatch,
    reject_reasons,
    validate_against_manifest,
)
from yizkr.pipeline.chunking import rejected_by_reason
from yizkr.pipeline.extract import salvage_valid_claims

UNIVERSE = by_key()
RUN = {
    "model_identifier": "fake-model",
    "pipeline_version": "0.1",
    "started_at": "2026-11-24T10:00:00+00:00",
}
# a stand-in for the SHA-1 of each source's media
ARTIFACTS = {
    "registration-card-1945": "a" * 40,
    "birth-extract-1938": "b" * 40,
    "naturalisation-1952": "c" * 40,
}
IMPRISONMENT = "imprisonment-1939"


def record(key: str) -> dict:
    """The entity-manifest record of a universe person, place or entity."""
    found = UNIVERSE[key]
    if isinstance(found, Person):
        kind = "human"
    elif isinstance(found, Place):
        kind = "place"
    else:
        assert isinstance(found, Entity)
        kind = found.kind
    return {"id": key, "label": found.label, "kind": kind}


ENTITIES = [
    record(key)
    for key in (
        IMPRISONMENT,
        "leopold-loebig",
        "peiltal-court-prison",
        "gustav-loebig",
        "australia",
        "felix-loebig",
        "wasserallee-59",
    )
]


def time(year: int, month: int | None = None, day: int | None = None):
    value: dict = {"type": "time", "year": year}
    if month is not None:
        value["month"] = month
    if day is not None:
        value["day"] = day
    return value


def item(local_id: str) -> dict:
    return {"type": "item", "value": {"kind": "local", "local_id": local_id}}


def claim(
    subject: str,
    prop: str,
    value: dict,
    *qualifiers: tuple[str, dict],
    source: str = "registration-card-1945",
) -> dict:
    return {
        "subject": {"kind": "local", "local_id": subject},
        "property": prop,
        "value": value,
        "qualifiers": [
            {"property": key, "value": given} for key, given in qualifiers
        ],
        "source": {
            "artifact": {"kind": "artifact", "sha1": ARTIFACTS[source]}
        },
    }


def codes(*claims: dict) -> list[str]:
    """The reason codes validation gives the claims' problems."""
    return reject_reasons(
        validate_against_manifest(
            ClaimBatch.model_validate(
                {
                    "extraction_run": RUN,
                    "claims": list(claims),
                    "entities": ENTITIES,
                }
            )
        )
    )


def imprisonment_start(*qualifiers: tuple[str, dict]) -> dict:
    """The imprisonment's own start, a date value: September 1939."""
    return claim(IMPRISONMENT, "start_time", time(1939, 9), *qualifiers)


def imprisonment_end(*qualifiers: tuple[str, dict]) -> dict:
    """The imprisonment's own end, a date value: October 1939."""
    return claim(IMPRISONMENT, "end_time", time(1939, 10), *qualifiers)


class TestAnEntitysOwnStartOrEnd:
    def test_a_latest_start_date_is_refused(self):
        assert codes(
            imprisonment_start(("latest_start_date", time(1939, 9, 5)))
        ) == [PERIOD_BOUND]

    def test_an_earliest_end_date_alone_is_refused(self):
        assert codes(
            imprisonment_end(("earliest_end_date", time(1939, 10, 1)))
        ) == [PERIOD_BOUND]

    def test_a_range_beside_its_own_bound_is_still_refused(self):
        assert codes(
            imprisonment_start(
                ("earliest_date", time(1939, 9, 1)),
                ("latest_start_date", time(1939, 9, 5)),
            )
        ) == [PERIOD_BOUND]

    def test_its_earliest_and_latest_date_pass(self):
        assert (
            codes(
                imprisonment_start(
                    ("earliest_date", time(1939, 9, 1)),
                    ("latest_date", time(1939, 9, 5)),
                ),
                imprisonment_end(
                    ("earliest_date", time(1939, 10, 1)),
                    ("latest_date", time(1939, 10, 31)),
                ),
            )
            == []
        )


class TestOtherDatesTakeNoPeriodBound:
    """The widest form: no claim without a period's start or end carries
    one of its bounds."""

    @pytest.mark.parametrize(
        "bound", ["latest_start_date", "earliest_end_date"]
    )
    def test_a_date_of_birth(self, bound):
        assert codes(
            claim(
                "gustav-loebig",
                "date_of_birth",
                time(1919, 1, 7),
                (bound, time(1919, 1, 7)),
                source="birth-extract-1938",
            )
        ) == [PERIOD_BOUND]

    @pytest.mark.parametrize(
        "bound", ["latest_start_date", "earliest_end_date"]
    )
    def test_a_point_in_time(self, bound):
        assert codes(
            claim(
                IMPRISONMENT,
                "point_in_time",
                time(1939, 9),
                (bound, time(1939, 9, 5)),
            )
        ) == [PERIOD_BOUND]

    def test_an_observation_dated_by_a_point_in_time(self):
        assert codes(
            claim(
                "felix-loebig",
                "residence",
                item("wasserallee-59"),
                ("point_in_time", time(1930)),
                ("earliest_end_date", time(1930)),
            )
        ) == [PERIOD_BOUND]


class TestAPeriodTakesTheBoundsOfItsEnds:
    def test_a_start_with_its_range(self):
        assert (
            codes(
                claim(
                    "gustav-loebig",
                    "residence",
                    item("australia"),
                    ("start_time", time(1939)),
                    ("earliest_date", time(1938)),
                    ("latest_start_date", time(1939)),
                    source="naturalisation-1952",
                )
            )
            == []
        )

    def test_an_end_with_its_range(self):
        assert (
            codes(
                claim(
                    "felix-loebig",
                    "residence",
                    item("wasserallee-59"),
                    ("end_time", time(1931)),
                    ("earliest_end_date", time(1930)),
                    ("latest_date", time(1931)),
                )
            )
            == []
        )

    def test_a_start_bound_beside_an_end_alone(self):
        # a period whose end is stated, and whose start is known only to
        # be no later than an observation of it
        assert (
            codes(
                claim(
                    "felix-loebig",
                    "residence",
                    item("wasserallee-59"),
                    ("end_time", time(1931)),
                    ("latest_start_date", time(1930)),
                )
            )
            == []
        )

    def test_an_end_bound_beside_a_start_alone(self):
        assert (
            codes(
                claim(
                    "gustav-loebig",
                    "residence",
                    item("australia"),
                    ("start_time", time(1939)),
                    ("earliest_end_date", time(1952)),
                    source="naturalisation-1952",
                )
            )
            == []
        )


class TestSalvage:
    def test_the_claim_is_a_counted_reject_and_the_others_stand(self):
        participant = claim(
            IMPRISONMENT, "participant", item("leopold-loebig")
        )
        location = claim(
            IMPRISONMENT, "location", item("peiltal-court-prison")
        )
        stray = imprisonment_start(("latest_start_date", time(1939, 9, 5)))
        batch, rejects = salvage_valid_claims(
            [participant, stray, location], RUN, entities=ENTITIES
        )
        assert [claim.property for claim in batch.claims] == [
            "participant",
            "location",
        ]
        assert [reject["claim"] for reject in rejects] == [stray]
        assert [reject["reasons"] for reject in rejects] == [[PERIOD_BOUND]]
        assert rejected_by_reason(rejects) == {PERIOD_BOUND: 1}
