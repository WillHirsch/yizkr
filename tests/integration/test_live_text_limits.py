"""Live validation of texts over the limit Wikibase holds in a value
(#366; the writer's TEXT_LIMIT and `_locator_text`).

Wikibase holds at most 400 characters in the text of a string,
external-id or monolingual text value, a reference's page number among
them, and refuses the whole edit that sends a longer one. A fictional
fixture writes one batch of four claims about Eugen Löbig, as (here) the
1909 burial register gives them, and reads them back through the API:

* the first claim gives his cause of death in 401 characters, and the
  third his daughter-in-law Hildegard's kinship to him in 450: the
  writer refuses both, names them in the result by their review-sheet
  ids and says why, and writes nothing of them, not even Hildegard's
  item, which only the third mentions. They come first and third, so a
  writer that stopped at a refusal would leave the others unwritten;
* the second gives his date of death on a page number of more than 400
  characters, given decomposed (NFD). The instance keeps it cut at a
  break between words, in NFC, ending " …": the leaf the limit falls
  inside is left out whole, rather than kept as another leaf's number;
* the fourth gives his cause of death in exactly 400 characters in NFC,
  more in the decomposed form the batch gives it in, on a page of
  exactly 400 characters in NFC, given decomposed too: the instance keeps
  both whole. So the writer's limit is the instance's, counted in the
  form the instance keeps text in;
* writing the batch again sends no edit at all: the cut page's block is
  found, and the same two claims are refused again.

Every entity's label carries a marker: six hex digits of a hash over the
fixture in NFC, its cited SHA-1 replaced by "A", and over the writer's
source, as test_live_text_forms.py's labels do, so each version of the
fixture and of the writer meets the fixture as a first write does. Each
entity has a description, so a writer that failed to find one by its
label could not mint a second item under it. The source is registered
with a fictional PNG of its own (register_fictional), which the claims
cite by SHA-1 (#102). Like every live test it writes fictional data to
the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest
from wikibaseintegrator.entities.baseentity import BaseEntity

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import CUT_MARK, TEXT_LIMIT, BatchWriter

from .conftest import register_fictional

pytestmark = pytest.mark.live


def said_over(phrase: str, length: int) -> str:
    """`phrase` said over and over, `length` characters long in NFC, the
    last of them a full stop."""
    text = (phrase * (length // len(phrase) + 1))[: length - 1] + "."
    assert len(unicodedata.normalize("NFC", text)) == length
    return text


def leaves(first: int, count: int) -> str:
    """(here) the leaves of the register's index that point to Eugen's
    entry, as an extraction gives them for its page number."""
    return " ; ".join(
        f"Blatt {leaf}, Flörburg" for leaf in range(first, first + count)
    )


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


ENTITIES = (
    {
        "id": "eugen",
        "label": "Text Limit Fixture Eugen Löbig",
        "kind": "human",
        "summary": "fixture: Eugen Löbig, master hatter of Flörburg",
    },
    {
        "id": "hildegard",
        "label": "Text Limit Fixture Hildegard Löbig",
        "kind": "human",
        "summary": "fixture: Hildegard Löbig, Eugen's daughter-in-law",
    },
)
KEYS = [entity["id"] for entity in ENTITIES]
# the cause of death as (here) the physician's note in the register gives
# it, over and over: over the limit, and at it
CAUSE = "Wassersucht nach langer Krankheit, Herzschwäche; "
CAUSE_OVER = said_over(CAUSE, TEXT_LIMIT + 1)
CAUSE_AT = said_over(CAUSE, TEXT_LIMIT)
assert len(nfd(CAUSE_AT)) > TEXT_LIMIT
# Hildegard's kinship to Eugen, the register's words over and over
KINSHIP = said_over("Schwiegertochter des Verstorbenen, ", 450)
# a page over the limit, whose room ends inside leaf 21: kept in NFC up to
# the leaf before it, and the mark
PAGE_OVER = leaves(2, 40)
assert PAGE_OVER[396:399] == " 21"
PAGE_CUT = PAGE_OVER[:396] + CUT_MARK
assert PAGE_CUT.endswith("Blatt 20, Flörburg ; Blatt …")
# a page at the limit in NFC, ending on a whole leaf
PAGE_AT = leaves(85, 40)[:TEXT_LIMIT]
assert PAGE_AT.endswith("Blatt 103, Flörburg")
assert len(nfd(PAGE_AT)) > TEXT_LIMIT
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-29T12:00:00Z",
}


def batch_content(sha1: str, labels: dict[str, str]) -> dict:
    """The fixture's batch in NFC, citing the media `sha1`, its entities
    under `labels`."""

    def ref(key: str) -> dict:
        return {
            "kind": "local",
            "local_id": key,
            "label": labels[key],
            "instance_of": "human",
        }

    def cites(excerpt: str, page: str | None = None) -> dict:
        source = {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        }
        if page:
            source["page_number"] = page
        return source

    def cause(text: str, page: str | None = None) -> dict:
        return {
            "subject": ref("eugen"),
            "property": "cause_of_death",
            "value": {
                "type": "monolingual",
                "value": {"text": text, "language": "de"},
            },
            "source": cites("Todesursache: Wassersucht.", page),
        }

    return {
        "extraction_run": RUN,
        "claims": [
            cause(CAUSE_OVER),
            {
                "subject": ref("eugen"),
                "property": "date_of_death",
                "value": {"type": "time", "year": 1909, "month": 3, "day": 16},
                "source": cites(
                    "Gestorben: 16. März 1909 in Flörburg.", PAGE_OVER
                ),
            },
            {
                "subject": ref("hildegard"),
                "property": "relative",
                "value": {"type": "item", "value": ref("eugen")},
                "qualifiers": [
                    {
                        "property": "kinship_type",
                        "value": {"type": "string", "value": KINSHIP},
                    }
                ],
                "source": cites("Angezeigt von der Schwiegertochter."),
            },
            cause(CAUSE_AT, PAGE_AT),
        ],
        "entities": [
            dict(entity, label=labels[entity["id"]]) for entity in ENTITIES
        ],
    }


def fixture_batch(artifact: ArtifactRef) -> ClaimBatch:
    """The fixture's batch under labels that carry the marker (the module
    docstring), with the date's page, the cause at the limit and its page
    given decomposed (NFD)."""
    plain = {entity["id"]: entity["label"] for entity in ENTITIES}
    marker = hashlib.sha256(
        json.dumps(batch_content("A", plain), sort_keys=True).encode()
        + Path(writer.__file__).read_bytes()
    ).hexdigest()[:6]
    labels = {key: f"{label} ({marker})" for key, label in plain.items()}
    content = batch_content(artifact.sha1, labels)
    _, died, _, at = content["claims"]
    died["source"]["page_number"] = nfd(PAGE_OVER)
    at["value"]["value"]["text"] = nfd(CAUSE_AT)
    at["source"]["page_number"] = nfd(PAGE_AT)
    return ClaimBatch.model_validate(content)


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch (idempotent: a re-run of this version
    finds and skips everything it writes, and refuses the same claims).
    Returns the source's item, the batch and the write's result."""
    registered = register_fictional(
        client, "Text limit fixture source", "artifact"
    )
    batch = fixture_batch(registered.ref)
    written = BatchWriter(client).write_batch(batch)
    return registered.qid, batch, written


def statements(client, qid: str, key: str) -> list[dict]:
    """The item's statements on `key`, as the API gives them."""
    properties, _ = client.vocabulary()
    found = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": properties[key]}
    )
    return found["claims"].get(properties[key], [])


def cited(client, statement: dict, key: str) -> list[str]:
    """What the statement's reference blocks give on `key`: an item's QID,
    or a string."""
    properties, _ = client.vocabulary()
    values = [
        snak["datavalue"]["value"]
        for block in statement.get("references", [])
        for snak in block["snaks"].get(properties[key], [])
    ]
    return [
        value["id"] if isinstance(value, dict) else value for value in values
    ]


def test_the_batch_is_written_around_the_refused_claims(client, fixture_graph):
    source, batch, written = fixture_graph
    # the first write of this version writes the two claims the instance
    # holds; a later session's write finds them there
    assert (written.written, written.referenced, written.skipped) in (
        (2, 0, 0),
        (0, 0, 2),
    )
    refused = [claim_id(batch.claims[0]), claim_id(batch.claims[2])]
    assert written.refused == refused
    over_cause, over_kinship = written.warnings
    assert over_cause.startswith(
        f"claim {refused[0]} (cause_of_death) is refused, and nothing of "
        f"it is written: its value has 401 characters"
    )
    assert over_kinship.startswith(
        f"claim {refused[1]} (relative) is refused, and nothing of it is "
        f"written: its kinship_type qualifier has 450 characters"
    )
    # Hildegard, whom only the refused kinship mentions, has no item
    assert "hildegard" not in written.created_items
    assert client.find_item_by_label(batch.entities[1].label) is None
    eugen = written.created_items["eugen"]
    # the date of death, on the page cut to the limit, in NFC
    (died,) = statements(client, eugen, "date_of_death")
    assert died["mainsnak"]["datavalue"]["value"]["time"] == (
        "+1909-03-16T00:00:00Z"
    )
    assert cited(client, died, "page_number") == [PAGE_CUT]
    assert len(PAGE_CUT) <= TEXT_LIMIT
    # the cause of death at the limit, whole, on its page at the limit,
    # whole; the cause over the limit is nowhere, whole or cut
    (cause,) = statements(client, eugen, "cause_of_death")
    assert cause["mainsnak"]["datavalue"]["value"] == {
        "text": CAUSE_AT,
        "language": "de",
    }
    assert cited(client, cause, "page_number") == [PAGE_AT]
    # each carries its source and its run (rules 1 and 2)
    for statement in (died, cause):
        assert cited(client, statement, "stated_in") == [source]
        assert cited(client, statement, "inferred_from_extraction_run") == [
            written.run_qid
        ]


def test_a_rewrite_sends_no_edit(client, fixture_graph, monkeypatch):
    _, batch, written = fixture_graph
    eugen = written.created_items["eugen"]
    before = {
        key: statements(client, eugen, key)
        for key in ("date_of_death", "cause_of_death")
    }
    edits: list[str | None] = []
    write = BaseEntity._write

    def counted(entity, *args, **kwargs):
        edits.append(entity.id)
        return write(entity, *args, **kwargs)

    monkeypatch.setattr(BaseEntity, "_write", counted)

    again = BatchWriter(client).write_batch(batch)

    assert edits == []
    assert again.run_qid == written.run_qid
    assert again.created_items["eugen"] == eugen
    assert again.refused == written.refused
    assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
    assert {
        key: statements(client, eugen, key)
        for key in ("date_of_death", "cause_of_death")
    } == before
