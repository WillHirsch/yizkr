"""Live validation of the printed page number on the write path (#424;
ADR-0037 §2, ADR-0003, docs/MODELING.md "Reference pattern").

A fictional fixture, the booklet *Our Family from Germany*, whose
physical page 6 is printed as page 4 (docs/EXAMPLES.md), writes two
claims read from it, as an extractor gives them, and reads the
statements back through the API:

* the claim read from page 6 has one source block: `stated in` the
  booklet, `page number` 6, the physical page, and `printed page number`
  4, beside its excerpt, and the run's block (rules 1 and 2);
* the claim read from its title page, page 1, which the booklet prints
  no number on, has a source block with its page number and no printed
  page number;
* writing the batch again adds nothing.

The instance must hold the manifest's `printed page number` property:
run `yizkr bootstrap` (or a scratch reset, which bootstraps) after the
manifest gains it, or every write here fails on the missing vocabulary.

The source is registered with a fictional PNG of its own
(register_fictional), which the claims cite by SHA-1 (#102). Like every
live test it writes fictional data to the one instance, so the session
that runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#424: the booklet's printed page naming Gustl, Ursula and Nicholas, "
    "its page numbered in print"
)
MARK = fixture_mark(FIXTURE, writer)

# the booklet's commentary on page 6: Gustl was Ursula's younger brother
GUSTL = {
    "kind": "local",
    "local_id": "printed-gustl",
    "label": "Printed Page Fixture Gustl",
    "instance_of": "human",
}
URSULA = {
    "kind": "local",
    "local_id": "printed-ursula",
    "label": "Printed Page Fixture Ursula",
    "instance_of": "human",
}
COMMENT = "Gustl was Ursula's younger brother."
# the title page, page 1, which carries no printed number, names its
# compiler
NICHOLAS = {
    "kind": "local",
    "local_id": "printed-nicholas",
    "label": "Printed Page Fixture Nicholas",
    "instance_of": "human",
}
TITLE = "Compiled by Nicholas Lobig."
RUN = "2026-10-02T09:00:00Z"


def claims(artifact: ArtifactRef) -> list[dict]:
    source = artifact.model_dump(mode="json")
    return [
        {
            "subject": GUSTL,
            "property": "sibling",
            "value": {"type": "item", "value": URSULA},
            "source": {
                "artifact": source,
                "page_number": "6",
                "printed_page_number": "4",
                "transcription_excerpt": {"text": COMMENT, "language": "en"},
            },
        },
        {
            "subject": NICHOLAS,
            "property": "referred_to_as",
            "value": {
                "type": "monolingual",
                "value": {"text": "Nicholas Lobig", "language": "en"},
            },
            "source": {
                "artifact": source,
                "page_number": "1",
                "transcription_excerpt": {"text": TITLE, "language": "en"},
            },
        },
    ]


def fixture_batch(artifact: ArtifactRef) -> ClaimBatch:
    """The fixture's batch, under labels that carry a marker: six hex
    digits of a hash over the claims, the cited SHA-1 replaced by "A",
    and over the module's marker (conftest.fixture_mark: the writer's
    source and the session's random bytes), as test_live_locators.py
    marks its labels. The writer finds a local item by its label, so each
    session, and each version of the fixture and of the writer, meets the
    fixture as a first write does: one that drops the printed page's snak
    (a mutant of the harness, say) cannot pass by finding the block an
    earlier session wrote, and what a live mutant's run wrote never meets
    a later session. A re-run within the session finds its own items
    again."""
    given = claims(artifact)
    content = [
        {
            **each,
            "source": {
                **each["source"],
                "artifact": {**each["source"]["artifact"], "sha1": "A"},
            },
        }
        for each in given
    ]
    marker = hashlib.sha256(
        json.dumps(content, sort_keys=True).encode() + MARK.encode()
    ).hexdigest()[:6]

    def mark(ref: dict) -> dict:
        return dict(ref, label=f"{ref['label']} ({marker})")

    marked = []
    for each in given:
        each = dict(each, subject=mark(each["subject"]))
        if each["value"]["type"] == "item":
            each["value"] = {"type": "item", "value": mark(URSULA)}
        marked.append(each)
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": RUN,
            },
            "claims": marked,
        }
    )


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch (idempotent: a re-run within the session
    reuses and skips everything). Returns the booklet's registration and
    the write's result."""
    registered = register_fictional(
        client, f"Our Family from Germany {MARK}", "artifact"
    )
    written = BatchWriter(client).write_batch(fixture_batch(registered.ref))
    return registered, written


def statements(client, item: str, key: str) -> list[dict]:
    """The item's statements of the manifest property `key`, as the API
    gives them."""
    properties, _ = client.vocabulary()
    found = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": item,
            "property": properties[key],
        }
    )
    return [
        statement
        for listed in found["claims"].values()
        for statement in listed
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


def test_the_printed_page_stands_beside_the_physical_one(
    client, fixture_graph
):
    registered, written = fixture_graph
    # the session's first write writes both claims
    assert (written.written, written.skipped) == (2, 0)
    (sibling,) = statements(
        client, written.created_items["printed-gustl"], "sibling"
    )
    assert sibling["rank"] == "normal"
    assert blocks(client, sibling) == [
        {
            "stated_in": [registered.qid],
            "page_number": ["6"],
            "printed_page_number": ["4"],
            "transcription_excerpt": [COMMENT],
        },
        {"inferred_from_extraction_run": [written.run_qid]},
    ]


def test_a_page_with_no_printed_number_has_none(client, fixture_graph):
    registered, written = fixture_graph
    (called,) = statements(
        client, written.created_items["printed-nicholas"], "referred_to_as"
    )
    assert blocks(client, called) == [
        {
            "stated_in": [registered.qid],
            "page_number": ["1"],
            "transcription_excerpt": [TITLE],
        },
        {"inferred_from_extraction_run": [written.run_qid]},
    ]


def test_a_rewrite_adds_nothing(client, fixture_graph):
    registered, written = fixture_graph
    written_to = {
        (written.created_items[local], key)
        for local, key in (
            ("printed-gustl", "sibling"),
            ("printed-nicholas", "referred_to_as"),
        )
    }
    before = {each: statements(client, *each) for each in written_to}
    again = BatchWriter(client).write_batch(fixture_batch(registered.ref))
    assert again.created_items == written.created_items
    assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
    assert {each: statements(client, *each) for each in written_to} == (before)
