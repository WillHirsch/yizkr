"""The review sheet shows a date at the precision its source states (#337).

`describe_value` is what the extraction critic renders values with before
the write, so its output is pinned byte for byte; the sheet has its own
formatter, `sheet_value`, which shows a century or a decade as the site
does ("the 19th century", "the 1890s"), never as its first year.
"""

import csv

import pytest

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    Qualifier,
    SourceReference,
    TimeValue,
)
from yizkr.pipeline.extract import run_metadata
from yizkr.pipeline.review import (
    describe_value,
    export_review_csv,
    sheet_value,
)

# the universe's studio portrait ("studio-photo-1890s"), fictional bytes
PORTRAIT_SCAN = ArtifactRef.from_bytes(b"fictional scan: a studio portrait")


@pytest.mark.parametrize(
    "value, shown",
    [
        (TimeValue(year=1801, precision="century"), "1801 (century)"),
        (TimeValue(year=1890, precision="decade"), "1890 (decade)"),
        (TimeValue(year=1833), "1833"),
        (TimeValue(year=1938, month=8), "1938-08"),
        (TimeValue(year=1939, month=7, day=28), "1939-07-28"),
        (TimeValue(year=850, month=3, day=7), "850-03-07"),
    ],
    ids=["century", "decade", "year", "month", "day", "before-1000"],
)
def test_describe_value_is_unchanged(value, shown):
    # the critic's prompt renders dates through describe_value, and the
    # critic runs before the write in scratch runs: a changed rendering
    # would change every critique call's cache key
    assert describe_value(value) == shown


@pytest.mark.parametrize(
    "value, shown",
    [
        (TimeValue(year=1801, precision="century"), "the 19th century"),
        (TimeValue(year=1901, precision="century"), "the 20th century"),
        (TimeValue(year=1890, precision="decade"), "the 1890s"),
        (TimeValue(year=1833), "1833"),
        (TimeValue(year=1938, month=8), "1938-08"),
        (TimeValue(year=1939, month=7, day=28), "1939-07-28"),
    ],
    ids=[
        "century",
        "next-century",
        "decade",
        "year",
        "month",
        "day",
    ],
)
def test_sheet_value_shows_a_date_at_its_precision(value, shown):
    assert sheet_value(value) == shown


def test_sheet_value_shows_any_other_value_as_describe_value_does():
    from yizkr.claims import MonolingualText, MonolingualValue

    name = MonolingualValue(
        value=MonolingualText(text="Rahel Löbig", language="de")
    )
    assert sheet_value(name) == describe_value(name) == "Rahel Löbig [de]"


def test_review_sheet_shows_the_portraits_decade_as_a_decade(tmp_path):
    # the portrait's own date, the 1890s, with a century-precision earliest
    # bound on it: neither is shown as its first year
    batch = ClaimBatch(
        extraction_run=run_metadata("fake-model"),
        claims=[
            Claim(
                subject=PORTRAIT_SCAN,
                property="inception",
                value=TimeValue(year=1890, precision="decade"),
                qualifiers=[
                    Qualifier(
                        property="earliest_date",
                        value=TimeValue(year=1801, precision="century"),
                    )
                ],
                source=SourceReference(artifact=PORTRAIT_SCAN),
            )
        ],
    )
    sheet = tmp_path / "review.csv"
    export_review_csv(batch, sheet)
    with open(sheet, newline="") as f:
        (row,) = csv.DictReader(f)
    assert row["value"] == "the 1890s"
    assert row["qualifiers"] == "earliest_date=the 19th century"


# -- an unknown value (ADR-0040 §2.2) -----------------------------------------

LETTER = ArtifactRef.from_bytes(b"fictional scan: a letter of 1938")


def late_felix(prop: str = "date_of_death") -> Claim:
    """A hypothetical letter's "der selige Felix": a death its source
    states without its date."""
    from yizkr.claims import LocalRef, MonolingualText, UnknownValue

    return Claim(
        subject=LocalRef(local_id="e1", label="Felix Löbig"),
        property=prop,
        value=UnknownValue(),
        source=SourceReference(
            artifact=LETTER,
            transcription_excerpt=MonolingualText(
                text="der selige Felix", language="de"
            ),
        ),
    )


def test_an_unknown_value_shows_as_one():
    from yizkr.claims import UnknownValue

    assert describe_value(UnknownValue()) == "unknown value"
    assert sheet_value(UnknownValue()) == "unknown value"


def test_the_sheet_shows_an_unknown_value_as_one(tmp_path):
    batch = ClaimBatch(
        extraction_run=run_metadata("fake-model"), claims=[late_felix()]
    )
    sheet = tmp_path / "review.csv"
    export_review_csv(batch, sheet)
    with open(sheet, newline="") as f:
        (row,) = csv.DictReader(f)
    assert row["value"] == "unknown value"


def test_a_curator_dates_an_unknown_death():
    # the curator's correction states the date the source gives
    from yizkr.pipeline.review import _parse_correction

    corrected = _parse_correction(late_felix(), "1931")
    assert isinstance(corrected.value, TimeValue)
    assert corrected.value.year == 1931 and corrected.curated is True
    # still the source's claim: the correction keeps its reference
    assert corrected.source == late_felix().source
    assert corrected.subject == late_felix().subject
    with pytest.raises(ValueError, match="not an unambiguous date"):
        _parse_correction(late_felix(), "long ago")


def test_an_unknown_burial_place_takes_no_text():
    # an item is fixed through the equivalence review, never as text
    from yizkr.pipeline.review import _parse_correction

    with pytest.raises(ValueError, match="cannot be corrected as text"):
        _parse_correction(late_felix("burial_place"), "Friedhof Flörburg")
