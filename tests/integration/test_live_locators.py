"""Live validation of one value that one artifact asserts in two places
(#335; ADR-0003, docs/MODELING.md "Reference pattern").

A fictional fixture writes one person's birth as one source gives it
twice, on its first page and on its second, each with an excerpt of its
own, and reads the statement back through the API:

* the person has one date-of-birth statement, and it carries a reference
  block for each page, each with its page number and excerpt, and the
  run's block once (rules 1 and 2);
* writing the batch again adds nothing, and nor does a later run's batch
  that finds the same two places: not even that run's block.

The source is registered with a fictional PNG of its own
(register_fictional), which the claims cite by SHA-1 (#102). Like every
live test it writes fictional data to the one instance, so the session
that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

from .conftest import register_fictional

pytestmark = pytest.mark.live

PERSON = {
    "kind": "local",
    "local_id": "locator-person",
    "label": "Locator Fixture Person",
    "instance_of": "human",
}
# the source gives the person's birth twice, in the words of the 1905
# marriage certificate: in the particulars on its first page, and (here)
# in the registrar's note on its second
EXCERPTS = {
    "1": "geboren am 15. Februar 1862 zu Flörburg",
    "2": "geb. 15.2.1862 in Flörburg",
}
# the fixture's run, and a later one that finds the same places
RUNS = ("2026-09-28T09:00:00Z", "2026-09-28T10:00:00Z")


def claim(artifact: ArtifactRef, page: str) -> dict:
    return {
        "subject": PERSON,
        "property": "date_of_birth",
        "value": {"type": "time", "year": 1862, "month": 2, "day": 15},
        "source": {
            "artifact": artifact.model_dump(mode="json"),
            "page_number": page,
            "transcription_excerpt": {
                "text": EXCERPTS[page],
                "language": "de",
            },
        },
    }


def fixture_batch(artifact: ArtifactRef, started_at: str) -> ClaimBatch:
    """The fixture's batch as the run started at `started_at` gives it,
    under a person label that carries a marker: six hex digits of a hash
    over the claims, the cited SHA-1 replaced by "A", as
    test_live_period_dates.py's write_fixture marks its labels, and over
    the writer's source. The writer finds a local item by its label, so a
    version of the fixture with other claims would otherwise write onto an
    earlier version's person and see its statements beside its own. The
    writer's source is hashed too, so that each version of the writer
    meets the fixture as a first write does: one that drops the second
    page's block (a mutant of the harness, say) cannot pass by finding the
    block an earlier version wrote. A re-run of one version finds its own
    person again, and a run started at the same time its own run item."""
    claims = [claim(artifact, page) for page in EXCERPTS]
    content = [
        {
            **each,
            "source": {
                **each["source"],
                "artifact": {**each["source"]["artifact"], "sha1": "A"},
            },
        }
        for each in claims
    ]
    marker = hashlib.sha256(
        json.dumps(content, sort_keys=True).encode()
        + Path(writer.__file__).read_bytes()
    ).hexdigest()[:6]
    person = dict(PERSON, label=f"{PERSON['label']} ({marker})")
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": started_at,
            },
            "claims": [dict(each, subject=person) for each in claims],
        }
    )


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch (idempotent: a re-run of this version
    reuses and skips everything). Returns the source's registration and
    the write's result."""
    registered = register_fictional(
        client, "Locator fixture source", "certificate"
    )
    written = BatchWriter(client).write_batch(
        fixture_batch(registered.ref, RUNS[0])
    )
    return registered, written


def births(client, person: str) -> list[dict]:
    """The person's date-of-birth statements, as the API gives them."""
    properties, _ = client.vocabulary()
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person,
            "property": properties["date_of_birth"],
        }
    )
    return [
        statement
        for statements in found["claims"].values()
        for statement in statements
    ]


def blocks(client, statement: dict) -> list[dict]:
    """Each reference block's values by manifest key, in the statement's
    order: an item's QID, a string, a monolingual text's text."""
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}

    def shown(value):
        if isinstance(value, dict):
            return value.get("id") or value["text"]
        return value

    return [
        {
            key_of[pid]: [shown(snak["datavalue"]["value"]) for snak in snaks]
            for pid, snaks in block["snaks"].items()
        }
        for block in statement.get("references", [])
    ]


def test_each_page_is_a_reference_block_of_its_own(client, fixture_graph):
    registered, written = fixture_graph
    # the first write of this version adds the second page's block to the
    # statement the first page's claim wrote; a later session's write
    # finds both there
    assert (written.written, written.referenced, written.skipped) in (
        (1, 1, 0),
        (0, 0, 2),
    )
    (born,) = births(client, written.created_items["locator-person"])
    assert born["mainsnak"]["datavalue"]["value"]["time"] == (
        "+1862-02-15T00:00:00Z"
    )
    assert born["rank"] == "normal"
    assert blocks(client, born) == [
        {
            "stated_in": [registered.qid],
            "page_number": ["1"],
            "transcription_excerpt": [EXCERPTS["1"]],
        },
        {"inferred_from_extraction_run": [written.run_qid]},
        {
            "stated_in": [registered.qid],
            "page_number": ["2"],
            "transcription_excerpt": [EXCERPTS["2"]],
        },
    ]


def test_a_rewrite_adds_nothing(client, fixture_graph):
    registered, written = fixture_graph
    person = written.created_items["locator-person"]
    before = births(client, person)
    for started_at in RUNS:
        again = BatchWriter(client).write_batch(
            fixture_batch(registered.ref, started_at)
        )
        assert again.created_items["locator-person"] == person
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
    assert births(client, person) == before
