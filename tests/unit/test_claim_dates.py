"""One date formatter for a claim's date (#617).

`core.dates.claim_time_text` renders a claim's TimeValue as the site
does, through `core.dates.time_text`; the review sheet (`sheet_value`),
the reconcile batch facts (`entity_facts`) and the adjudication prompt
(`adjudication.date_text`) all render a date with it, byte for byte as
they did before, and the review sheet and reconcile render one without
importing the adjudication stage.
"""

import subprocess
import sys

import pytest

from yizkr.claims import (
    ArtifactRef,
    Claim,
    ClaimBatch,
    LocalRef,
    SourceReference,
    TimeValue,
)
from yizkr.core.dates import claim_time_text, time_stamp, time_text
from yizkr.pipeline.adjudication import date_text
from yizkr.pipeline.extract import run_metadata
from yizkr.pipeline.reconcile import entity_facts
from yizkr.pipeline.review import sheet_value

# the universe's birth register extract, fictional bytes
REGISTER = ArtifactRef.from_bytes(b"fictional scan: a birth register extract")

# each date a claim can state, with its stamp, its Wikibase precision
# and how every reader renders it
DATES = [
    (
        {"year": 1801, "precision": "century"},
        "+1801-01-01T00:00:00Z",
        7,
        "the 19th century",
    ),
    (
        {"year": 1901, "precision": "century"},
        "+1901-01-01T00:00:00Z",
        7,
        "the 20th century",
    ),
    (
        {"year": 1830, "precision": "decade"},
        "+1830-01-01T00:00:00Z",
        8,
        "the 1830s",
    ),
    (
        {"year": 1835, "precision": "decade"},
        "+1835-01-01T00:00:00Z",
        8,
        "the 1830s",
    ),
    ({"year": 1911}, "+1911-01-01T00:00:00Z", 9, "1911"),
    (
        {"year": 1911, "precision": "year"},
        "+1911-01-01T00:00:00Z",
        9,
        "1911",
    ),
    ({"year": 1938, "month": 8}, "+1938-08-01T00:00:00Z", 10, "1938-08"),
    (
        {"year": 1919, "month": 1, "day": 7},
        "+1919-01-07T00:00:00Z",
        11,
        "1919-01-07",
    ),
    (
        {"year": 1919, "month": 7, "day": 1, "precision": "day"},
        "+1919-07-01T00:00:00Z",
        11,
        "1919-07-01",
    ),
    (
        {"year": 850, "month": 3, "day": 7},
        "+0850-03-07T00:00:00Z",
        11,
        "0850-03-07",
    ),
    ({"year": 850}, "+0850-01-01T00:00:00Z", 9, "850"),
]
IDS = [
    "century",
    "next-century",
    "decade",
    "decade-mid-year",
    "year",
    "year-stated",
    "month",
    "day",
    "day-stated",
    "day-before-1000",
    "year-before-1000",
]


@pytest.mark.parametrize("time, stamp, precision, shown", DATES, ids=IDS)
def test_a_claims_date_has_the_stamp_wikibase_stores(
    time, stamp, precision, shown
):
    assert time_stamp(TimeValue.model_validate(time)) == (stamp, precision)


@pytest.mark.parametrize("time, stamp, precision, shown", DATES, ids=IDS)
def test_a_claims_date_renders_as_its_stamp_does(
    time, stamp, precision, shown
):
    value = TimeValue.model_validate(time)
    assert claim_time_text(value, "a date") == shown
    assert time_text(stamp, precision, "a date") == shown


def facts_of(value: TimeValue) -> str:
    claim = Claim(
        subject=LocalRef(local_id="e1", label="Gustav Löbig"),
        property="date_of_birth",
        value=value,
        source=SourceReference(artifact=REGISTER),
    )
    batch = ClaimBatch(
        extraction_run=run_metadata("fake-model"), claims=[claim]
    )
    return entity_facts(batch, "e1")


@pytest.mark.parametrize("time, stamp, precision, shown", DATES, ids=IDS)
def test_every_reader_renders_a_date_alike(time, stamp, precision, shown):
    value = TimeValue.model_validate(time)
    assert date_text(value) == shown
    assert sheet_value(value) == shown
    assert facts_of(value) == f"date_of_birth: {shown}"


def test_the_sheet_and_reconcile_render_a_date_without_adjudication():
    # the adjudication stage imports clustering, orchestrate and llm; the
    # review sheet and the reconcile roster need none of them for a date
    code = """
import sys
from yizkr.claims import TimeValue
from yizkr.pipeline.review import sheet_value
from yizkr.pipeline import reconcile
value = TimeValue(year=1830, precision="decade")
assert sheet_value(value) == "the 1830s"
from yizkr.claims import ClaimBatch
reconcile.entity_facts(ClaimBatch.model_construct(claims=[]), "e1")
loaded = sorted(
    name for name in sys.modules
    if name in (
        "yizkr.pipeline.adjudication",
        "yizkr.pipeline.clustering",
        "yizkr.pipeline.orchestrate",
    )
)
print(loaded)
"""
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip() == "[]"
