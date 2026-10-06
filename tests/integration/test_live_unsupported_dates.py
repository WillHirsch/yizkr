"""Live: the writer refuses a date before the common era or in the Julian
calendar, and writes nothing (ADR-0034, #338).

The claim schema refuses such a date, so a batch can hold one only when it
is built around validation. Each case here builds a valid batch about a
fictional person, Eugen Löbig's birth (here) in 1836, then puts an
unsupported date in it with `model_construct`: as the claim's value
before the common era, as its value in the Julian calendar, or as its
earliest date before the common era. The writer raises UnsupportedDate
naming the claim, and afterwards the source is unedited, and neither the
person's item nor the batch's extraction-run item exists.

Like every live test it writes fictional data to the one instance (the
source's registration), so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.claims import ClaimBatch, TimeValue
from yizkr.core.dates import UnsupportedDate
from yizkr.pipeline import writer
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter, batch_fingerprint

from .conftest import fixture_mark, register_fictional

pytestmark = pytest.mark.live

FIXTURE = (
    "Eugen Löbig's 1836 birth, given before the common era, in the Julian "
    "calendar, or with an earliest date before the common era"
)
# this session's marker, so no earlier session's or live mutant's items
# are found (tests/integration/conftest.py, fixture_mark)
MARK = fixture_mark(FIXTURE, writer)
PERSON = f"Unsupported Date Fixture Eugen Löbig {MARK}"


def unsupported(fields: dict) -> TimeValue:
    """A time value built without validation, as no claim could carry."""
    return TimeValue.model_construct(
        type="time", month=None, day=None, precision="year", **fields
    )


# each case: where the unsupported date goes, and its fields
CASES = {
    "value before the common era": ("value", {"year": -1836}),
    "value in the Julian calendar": (
        "value",
        {"year": 1836, "calendar": "julian"},
    ),
    "earliest date before the common era": (
        "earliest_date",
        {"year": -1836},
    ),
}


@pytest.fixture(scope="module")
def source(client):
    return register_fictional(
        client, f"Unsupported date fixture source {MARK}", "certificate"
    )


def batch_with(source, case: str) -> ClaimBatch:
    """A valid birth claim, with the case's unsupported date put in."""
    where, fields = CASES[case]
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test-unsupported-dates",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "eugen",
                        "label": PERSON,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "qualifiers": [
                        {
                            "property": "earliest_date",
                            "value": {"type": "time", "year": 1836},
                        }
                    ],
                    "source": {
                        "artifact": source.ref.model_dump(mode="json"),
                        "page_number": "1",
                    },
                }
            ],
        }
    )
    (claim,) = batch.claims
    date = unsupported({"calendar": "gregorian", **fields})
    if where == "value":
        claim = claim.model_copy(update={"value": date})
    else:
        (qualifier,) = claim.qualifiers
        claim = claim.model_copy(
            update={
                "qualifiers": [qualifier.model_copy(update={"value": date})]
            }
        )
    return batch.model_copy(update={"claims": [claim]})


def last_revision(client, qid: str) -> int:
    info = client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": "info"}
    )
    return info["entities"][qid]["lastrevid"]


def run_item_label(batch: ClaimBatch) -> str:
    """The label the writer gives the batch's extraction-run item."""
    run = batch.extraction_run
    return (
        f"extraction run {run.started_at.date().isoformat()} "
        f"{run.model_identifier} {batch_fingerprint(batch)}"
    )


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_writer_refuses_an_unsupported_date(client, source, case):
    batch = batch_with(source, case)
    revision = last_revision(client, source.qid)

    with pytest.raises(UnsupportedDate) as refusal:
        write_mapped(BatchWriter(client), batch)

    (claim,) = batch.claims
    where, _ = CASES[case]
    part = "its value" if where == "value" else f"its {where} qualifier"
    assert (
        f"claim {claim_id(claim)} (date_of_birth), {part}: unsupported: "
        in str(refusal.value)
    )
    assert "nothing was written" in str(refusal.value)
    # nothing was written: the source unedited, no person, no run item
    assert last_revision(client, source.qid) == revision
    assert client.find_item_by_label(PERSON) is None
    assert client.find_item_by_label(run_item_label(batch)) is None
