"""Live validation of the form the writer sends terms and text in (#369,
#403; the writer's `stored_term`, `_cleaned_up` and CLEANED_UP).

Wikibase keeps a label, a description or an alias as its trimToNFC gives
it: trimmed, then cleaned up (UtfNormal's cleanUp makes each C0 control
character but the tab, the line feed and the carriage return, and the
noncharacters U+FFFE and U+FFFF, U+FFFD), then in NFC. It cleans up a
string value before it stores it, and every text its API gives back, a
monolingual text among them. A fictional fixture writes one batch of five
claims about Eugen Löbig, his daughter-in-law Hildegard and the family's
firm, as (here) the 1909 burial register and a scan's text layer give
them, and reads them back through the API:

* Eugen's label is given with spaces before it and an ideographic space
  after it: the instance keeps it trimmed, and a second write finds his
  item by it and mints no other. He has a description, as Hildegard has,
  so a write that missed his item would be refused as a second item
  with his label and description, and leave none behind: two items with
  one label and no description would make every later label lookup on
  the instance fail (WikiClient.label_index);
* Hildegard's label ends in U+FFFF, which the instance keeps as U+FFFD,
  and a second write finds her item by it;
* the first claim gives Eugen's date of death on a page with a backspace
  (U+0008) in it, with an excerpt holding U+001F: the instance gives both
  back with U+FFFD in their place, and a second write finds the block;
* the firm's type hint holds U+0001 and ends in U+FFFE: the instance
  gives it back with U+FFFD in their place, and a second write adds no
  type statement;
* the fourth claim gives Eugen's nickname with U+0001 in it, and the fifth
  his age at death with U+001F in it: the writer refuses both, names them
  by their review-sheet ids, says why, quoting no text, and writes
  nothing of them;
* writing the batch again sends no edit at all, and refuses the same two
  claims.

Every entity's label carries a marker: six hex digits of a hash over the
fixture, its cited SHA-1 replaced by "A", and over the writer's source, as
test_live_text_shapes.py's labels do, so each version of the fixture and
of the writer meets the fixture as a first write does. The source is
registered with a fictional PNG of its own (register_fictional), which the
claims cite by SHA-1 (#102). Like every live test it writes fictional data
to the one instance, so the session that runs it ends by clearing the
recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from wikibaseintegrator.entities.baseentity import BaseEntity

from yizkr.claims import ClaimBatch
from yizkr.pipeline import writer
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter

from .conftest import register_fictional

pytestmark = pytest.mark.live

REPLACEMENT = "\N{REPLACEMENT CHARACTER}"
# the page and excerpt of Eugen's date of death, and the firm's type hint,
# as the batch gives them and as the instance gives them back
PAGE = "Blatt\x0812, Flörburg"
PAGE_KEPT = f"Blatt{REPLACEMENT}12, Flörburg"
EXCERPT = "Gestorben:\x1f16. März 1909."
EXCERPT_KEPT = f"Gestorben:{REPLACEMENT}16. März 1909."
TYPE_HINT = "Hut\x01geschäft\ufffe"
TYPE_HINT_KEPT = f"Hut{REPLACEMENT}geschäft{REPLACEMENT}"
ENTITIES = (
    {
        "id": "eugen",
        "label": "Eugen Löbig",
        "kind": "human",
        "summary": "fixture: Eugen Löbig, hatter, who founded the Blue Hat",
    },
    {
        "id": "hildegard",
        "label": "Hildegard Löbig",
        "kind": "human",
        "summary": "fixture: Hildegard Löbig, Eugen's daughter-in-law",
    },
    {
        "id": "firm",
        "label": "Hutmacherei zum Blauen Hut",
        "kind": "unclassified_entity",
        "type_hint": TYPE_HINT,
    },
)
RUN = {
    "model_identifier": "integration test stored term fixture model",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-29T12:00:00Z",
}


def batch_content(sha1: str, labels: dict[str, str]) -> dict:
    """The fixture's batch, citing the media `sha1`, its entities under
    `labels`."""
    kinds = {entity["id"]: entity["kind"] for entity in ENTITIES}

    def ref(key: str) -> dict:
        return {
            "kind": "local",
            "local_id": key,
            "label": labels[key],
            "instance_of": kinds[key],
        }

    def cites(excerpt: str, page: str | None = None) -> dict:
        source = {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        }
        if page:
            source["page_number"] = page
        return source

    return {
        "extraction_run": RUN,
        "claims": [
            {
                "subject": ref("eugen"),
                "property": "date_of_death",
                "value": {"type": "time", "year": 1909, "month": 3, "day": 16},
                "source": cites(EXCERPT, PAGE),
            },
            {
                "subject": ref("hildegard"),
                "property": "relative",
                "value": {"type": "item", "value": ref("eugen")},
                "qualifiers": [
                    {
                        "property": "kinship_type",
                        "value": {
                            "type": "string",
                            "value": "Schwiegertochter",
                        },
                    }
                ],
                "source": cites("Angezeigt von der Schwiegertochter."),
            },
            {
                "subject": ref("firm"),
                "property": "start_time",
                "value": {"type": "time", "year": 1863},
                "source": cites("gegr. 1863"),
            },
            {
                "subject": ref("eugen"),
                "property": "nickname",
                "value": {
                    "type": "monolingual",
                    "value": {"text": "Mu\x01ckel", "language": "de"},
                },
                "source": cites("Eugen Löbig, genannt Muckel."),
            },
            {
                "subject": ref("eugen"),
                "property": "age_at_death",
                "value": {"type": "string", "value": "73\x1fJahre"},
                "source": cites("Alter: 73 Jahre."),
            },
        ],
        "entities": [
            dict(entity, label=labels[entity["id"]]) for entity in ENTITIES
        ],
    }


def fixture_marker() -> str:
    """The marker (the module docstring)."""
    plain = {entity["id"]: entity["label"] for entity in ENTITIES}
    return hashlib.sha256(
        json.dumps(batch_content("A", plain), sort_keys=True).encode()
        + Path(writer.__file__).read_bytes()
    ).hexdigest()[:6]


def labels_of(marker: str) -> dict[str, str]:
    """Each entity's label as the fixture's refs and records give it, the
    marker after the fixture's name: Eugen's with spaces before it and an
    ideographic space after it, Hildegard's ending in U+FFFF."""
    return {
        "eugen": f"  Stored Term Fixture ({marker}) Eugen Löbig　",
        "hildegard": f"Stored Term Fixture ({marker}) Hildegard Löbig\uffff",
        "firm": f"Stored Term Fixture ({marker}) Hutmacherei zum Blauen Hut",
    }


def labels_kept(marker: str) -> dict[str, str]:
    """Each entity's label as the instance keeps it."""
    return {
        "eugen": f"Stored Term Fixture ({marker}) Eugen Löbig",
        "hildegard": (
            f"Stored Term Fixture ({marker}) Hildegard Löbig{REPLACEMENT}"
        ),
        "firm": f"Stored Term Fixture ({marker}) Hutmacherei zum Blauen Hut",
    }


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch (idempotent: a re-run of this version
    finds and skips everything it writes, and refuses the same claims).
    Returns the source's item, the batch and the write's result."""
    registered = register_fictional(
        client, "Stored term fixture source", "artifact"
    )
    batch = ClaimBatch.model_validate(
        batch_content(registered.ref.sha1, labels_of(fixture_marker()))
    )
    written = BatchWriter(client).write_batch(batch)
    return registered.qid, batch, written


def statements(client, qid: str, key: str) -> list[dict]:
    """The item's statements on `key`, as the API gives them."""
    properties, _ = client.vocabulary()
    found = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": properties[key]}
    )
    return found["claims"].get(properties[key], [])


def cited(client, statement: dict, key: str) -> list:
    """What the statement's reference blocks give on `key`: an item's QID,
    a monolingual text's text, or a string."""
    properties, _ = client.vocabulary()
    values = [
        snak["datavalue"]["value"]
        for block in statement.get("references", [])
        for snak in block["snaks"].get(properties[key], [])
    ]
    return [
        value.get("id", value.get("text"))
        if isinstance(value, dict)
        else value
        for value in values
    ]


def labels(client, qids: list[str]) -> dict[str, str | None]:
    """Each item's English label, as the API gives it."""
    found = client.api_get(
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels",
            "languages": "en",
        }
    )["entities"]
    return {
        qid: entity.get("labels", {}).get("en", {}).get("value")
        for qid, entity in found.items()
    }


def test_terms_and_text_as_the_instance_keeps_them(client, fixture_graph):
    source, batch, written = fixture_graph
    # the first write of this version writes the date of death, the
    # kinship and the firm's founding; a later session's finds all three
    first = (written.written, written.referenced, written.skipped)
    assert first in ((3, 0, 0), (0, 0, 3))
    refused = [claim_id(batch.claims[index]) for index in (3, 4)]
    assert written.refused == refused
    for warning, cid, key in zip(
        written.warnings, refused, ("nickname", "age_at_death"), strict=True
    ):
        assert warning == (
            f"claim {cid} ({key}) is refused, and nothing of it is written: "
            f"its value holds a control character or a noncharacter, which "
            f"Wikibase changes into U+FFFD. A cut or any other change would "
            f"alter what the source says"
        )
        assert "Muckel" not in warning and "Jahre" not in warning
    eugen = written.created_items["eugen"]
    hildegard = written.created_items["hildegard"]
    firm = written.created_items["firm"]
    assert len({eugen, hildegard, firm}) == 3
    # each label as the instance keeps it, naming one item
    kept = labels_kept(fixture_marker())
    assert labels(client, [eugen, hildegard, firm]) == {
        eugen: kept["eugen"],
        hildegard: kept["hildegard"],
        firm: kept["firm"],
    }
    for key, qid in (("eugen", eugen), ("hildegard", hildegard)):
        assert client.find_item_by_label(kept[key]) == qid
    # nothing of the refused claims is on the instance
    for key in ("nickname", "age_at_death"):
        assert statements(client, eugen, key) == []
    # the date of death: its page and excerpt given back cleaned up, and
    # both references (rules 1 and 2)
    (died,) = statements(client, eugen, "date_of_death")
    assert cited(client, died, "page_number") == [PAGE_KEPT]
    assert cited(client, died, "transcription_excerpt") == [EXCERPT_KEPT]
    assert cited(client, died, "stated_in") == [source]
    assert cited(client, died, "inferred_from_extraction_run") == [
        written.run_qid
    ]
    # the firm's type hint, given back cleaned up
    (hint,) = statements(client, firm, "type_hint")
    assert hint["mainsnak"]["datavalue"]["value"] == TYPE_HINT_KEPT
    assert cited(client, hint, "stated_in") == [source]


def test_a_rewrite_sends_no_edit(client, fixture_graph, monkeypatch):
    _, batch, written = fixture_graph
    qids = [
        written.created_items["eugen"],
        written.created_items["hildegard"],
        written.created_items["firm"],
    ]
    keys = ("date_of_death", "relative", "type_hint", "instance_of")
    before = (
        labels(client, qids),
        [statements(client, qid, key) for qid in qids for key in keys],
    )
    edits: list[str | None] = []
    write = BaseEntity._write

    def counted(entity, *args, **kwargs):
        edits.append(entity.id)
        return write(entity, *args, **kwargs)

    monkeypatch.setattr(BaseEntity, "_write", counted)

    again = BatchWriter(client).write_batch(batch)

    assert edits == []
    assert again.run_qid == written.run_qid
    assert again.created_items == written.created_items
    assert again.refused == written.refused
    assert (again.written, again.referenced, again.skipped) == (0, 0, 3)
    assert (
        again.type_statements_written,
        again.type_statements_referenced,
    ) == (
        0,
        0,
    )
    assert (
        labels(client, qids),
        [statements(client, qid, key) for qid in qids for key in keys],
    ) == before
