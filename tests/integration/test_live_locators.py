"""Live validation of one value that one artifact asserts in two places
(#335; ADR-0003, docs/MODELING.md "Reference pattern").

A fictional fixture writes one person's birth as one source gives it
twice, on its first page and on its back, each with an excerpt of its
own, and reads the statement back through the API:

* the person has one date-of-birth statement, and it carries a reference
  block for each page, each with its page number and excerpt, and the
  run's block once (rules 1 and 2);
* the back's page number and the note's words, both given in
  decomposed form (NFD), are stored in NFC: the writer sends a
  reference's text in NFC, Wikibase keeps a string value in NFC, and
  keeps a monolingual text as sent;
* writing the batch again adds nothing, and nor does a later run's batch
  that finds the same two places: not even that run's block;
* the site's artifact page and the `artifact-assertions` query show the
  statement once and each place the source gives it, its page number and
  excerpt (#351): the site's query gives a row for each block, and the
  page one row with both places, its count counting the statement once.

The source is registered with a fictional PNG of its own
(register_fictional), which the claims cite by SHA-1 (#102). Its title and
the person's label carry the session's marker (conftest.fixture_mark), so
each session meets the fixture as a first write. Like every live test it
writes fictional data to the one instance, so the session that runs it ends
by clearing the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata

import pytest

from tests.fixtures.identity import write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance
from yizkr.staticsite import (
    ARTIFACTS_QUERY,
    ASSERTIONS_QUERY,
    PEOPLE_QUERY,
    SiteBuilder,
    cell,
    qid_of,
)

from .conftest import (
    fixture_mark,
    register_fictional,
    run_sparql,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#335: one person's birth as the 1905 marriage certificate gives it "
    "twice, on its first page and on its back, written by two runs"
)
MARK = fixture_mark(FIXTURE, writer)

PERSON = {
    "kind": "local",
    "local_id": "locator-person",
    "label": "Locator Fixture Person",
    "instance_of": "human",
}
# the source gives the person's birth twice, in the words of the 1905
# marriage certificate: in the particulars on its first page, and (here)
# in the registrar's note on its back, whose page number and words are
# given in decomposed form (NFD)
FRONT = "1"
BACK = unicodedata.normalize("NFD", "Rückseite")
EXCERPTS = {
    FRONT: "geboren am 15. Februar 1862 zu Flörburg",
    BACK: unicodedata.normalize("NFD", "geb. 15.2.1862 in Flörburg"),
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
    the module's marker (conftest.fixture_mark: the writer's source and the
    session's random bytes). The writer finds a local item by its label,
    so a version of the fixture with other claims would otherwise write
    onto an earlier version's person and see its statements beside its
    own. The writer's source and the session are hashed too, so that each
    session, and each version of the writer, meets the fixture as a first
    write does: one that drops the back's block (a mutant of the harness,
    say) cannot pass by finding the block an earlier session wrote, and
    what a live mutant's run wrote never meets a later session. A re-run
    within the session finds its own person again, and a run started at
    the same time its own run item."""
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
        json.dumps(content, sort_keys=True).encode() + MARK.encode()
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
    """Write the fixture's batch (idempotent: a re-run within the
    session reuses and skips everything). Returns the source's
    registration and the write's result."""
    registered = register_fictional(
        client, f"Locator fixture source {MARK}", "certificate"
    )
    written = write_mapped(
        BatchWriter(client), fixture_batch(registered.ref, RUNS[0])
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
    # the session's first write adds the back's block to the statement
    # the first page's claim wrote
    assert (written.written, written.referenced, written.skipped) == (1, 1, 0)
    (born,) = births(client, written.created_items["locator-person"])
    assert born["mainsnak"]["datavalue"]["value"]["time"] == (
        "+1862-02-15T00:00:00Z"
    )
    assert born["rank"] == "normal"
    # the back's page number and excerpt are stored in NFC
    assert unicodedata.normalize("NFC", BACK) != BACK
    assert unicodedata.normalize("NFC", EXCERPTS[BACK]) != EXCERPTS[BACK]
    assert blocks(client, born) == [
        {
            "stated_in": [registered.qid],
            "page_number": [FRONT],
            "transcription_excerpt": [EXCERPTS[FRONT]],
        },
        {"inferred_from_extraction_run": [written.run_qid]},
        {
            "stated_in": [registered.qid],
            "page_number": [unicodedata.normalize("NFC", BACK)],
            "transcription_excerpt": [
                unicodedata.normalize("NFC", EXCERPTS[BACK])
            ],
        },
    ]


def test_a_rewrite_adds_nothing(client, fixture_graph):
    registered, written = fixture_graph
    person = written.created_items["locator-person"]
    before = births(client, person)
    for started_at in RUNS:
        again = write_mapped(
            BatchWriter(client), fixture_batch(registered.ref, started_at)
        )
        assert again.created_items["locator-person"] == person
        assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
    assert births(client, person) == before


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def test_the_site_and_the_query_show_each_place(
    client, config, fixture_graph, tmp_path
):
    registered, written = fixture_graph
    person = written.created_items["locator-person"]
    wait_for_query_service(config, client, [person, registered.qid])
    properties, items = client.vocabulary()
    entity = f"{config.concept_uri}/entity/"
    # each page the source gives the birth at, with its excerpt, stored in
    # NFC
    places = sorted((nfc(page), nfc(text)) for page, text in EXCERPTS.items())

    def ours(row: dict) -> bool:
        return cell(row, "artifact") == entity + registered.qid

    # the site's query: a row for each block, of one statement
    builder = SiteBuilder(config, tmp_path, vocabulary=(properties, items))
    rows = builder.run(builder._instance_query(ASSERTIONS_QUERY))
    births = [
        row
        for row in rows
        if ours(row)
        and cell(row, "subject") == entity + person
        and cell(row, "propLabel") == "date of birth"
    ]
    (statement,) = {cell(row, "st") for row in births}
    assert statement.startswith(f"{config.concept_uri}/entity/statement/")
    assert (
        sorted((cell(row, "page"), cell(row, "excerpt")) for row in births)
        == places
    )
    assert {row["excerpt"].get("xml:lang") for row in births} == {"de"}

    # the page: the statement once, both places in its row. The evidence
    # table links a subject to its page only where the site gives it one,
    # by the roster and the artifacts build() reads before it writes a
    # page (#701), so the call reads them as build() does
    builder.people = {
        qid_of(row, "person"): row
        for row in builder.run(builder._instance_query(PEOPLE_QUERY))
    }
    builder.artifact_qids = {
        qid_of(row, "artifact")
        for row in builder.run(builder._instance_query(ARTIFACTS_QUERY))
    }
    assert person in builder.people
    assert registered.qid in builder.artifact_qids
    page = builder.artifact_page(
        {"artifact": {"type": "uri", "value": entity + registered.qid}},
        [row for row in rows if ours(row)],
    )
    table = re.findall(r"<tr><td>(.*?)</tr>", page)
    (count,) = re.findall(r"What this artifact asserts \((\d+)\)", page)
    assert int(count) == len(table)
    (row,) = [
        row
        for row in table
        if f'href="../people/{person}.html"' in row
        and "<td>date of birth</td>" in row
    ]
    assert row.endswith(
        "<td>"
        + "<br>".join(
            f'page number: {html.escape(number)}: <q lang="de">'
            f"{html.escape(text)}</q>"
            for number, text in places
        )
        + "</td>"
    )

    # the library query: the statement, and a row for each place
    query = render_for_instance(
        load_templates()["artifact-assertions"],
        properties,
        items,
        config.concept_uri,
    )
    listed = [
        row
        for row in run_sparql(config, query)
        if ours(row)
        and cell(row, "entity") == entity + person
        and cell(row, "propertyLabel") == "date of birth"
    ]
    # the same statement as the site's query gives
    assert {cell(row, "statement") for row in listed} == {
        cell(row, "st") for row in births
    }
    assert (
        sorted((cell(row, "page"), cell(row, "excerpt")) for row in listed)
        == places
    )
