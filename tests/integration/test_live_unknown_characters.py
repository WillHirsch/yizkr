"""Live validation of the characters the writer's Unicode tables do not
know (#431; the writer's `unknown_characters` and INSTANCE_UNICODE).

The instance puts text in NFC by the tables of Unicode 15.0 (ICU 72.1 in
the wikibase/wikibase:7 image, reached through UtfNormal 4.0.0 and PHP's
intl), and the writer by Python's, 14.0.0 on Python 3.11. They differ only
on text holding a character Unicode assigned after 14.0.0, such as
U+1E08F, the combining Cyrillic letter Byelorussian-Ukrainian i: on a
Cyrillic a with a dot below after it, the instance's NFC puts the dot
first. A copy of Nina Sartina's 1958 letter, whose transcriber
wrote her surname with the i over its a and a dot below it, gives:

* Nina, by a fictional label without the mark, with an alias and a
  summary that hold it, and an alias that does not: her item is minted
  with her label, the clean alias and the writer's source descriptor as
  its description (#86), and the marked alias is left out, named in the
  result's `refused_terms`; the summary is never sent, so it is not;
* her married name, a clean claim, which is written with both its
  references;
* her kinship to Rudolf, its kinship_type qualifier, a string value,
  holding the mark: the claim is refused whole, and Rudolf, whom only it
  mentions, gets no item;
* writing the batch again sends no edit at all, and refuses and leaves
  out the same.

A third test writes a term holding the mark directly, bypassing the
writer, as the alias of an item of its own, and reads it back: the
instance keeps it with the dot before the i, a form other than Python's
NFC, which is why the writer refuses such text. It is the precondition of
the second: on an instance that kept the mark in Python's order, a
rewrite would send no edit whatever the writer refused. Every test here
is skipped on an interpreter whose tables know U+1E08F.

A fourth test registers two copies of the letter's scan, different bytes
under one file name in two folders, with one title that holds the mark
and one description, as the orchestrator gives them: the instance keeps
the first one's label in its own order, so the registrar cannot tell
whether the label is free, and gives each copy's label its content tag.
Both register, each write makes no item but the two artifacts, and a
second registration sends no edit. Their bytes and title carry a marker
over the registrar's source too, so each version of the registrar meets
them as a first registration does.

Every entity's label carries a marker: six hex digits of a hash over the
fixture, its cited SHA-1 replaced by "A", and over the module's marker
(conftest.fixture_mark: the writer's source and the session's random
bytes), as test_live_stored_terms.py's labels do, so each session, and each
version of the fixture and of the writer, meets the fixture as a first
write does: what an earlier session wrote, a live mutant's run included,
never meets it. The source is registered with a fictional PNG of its own
(register_fictional), which the claims cite by SHA-1 (#102). Like every
live test it writes fictional data to the one instance, so the session that
runs it ends by clearing the recorded instance-contents key
(docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from pathlib import Path

import pytest
from wikibaseintegrator.entities.baseentity import BaseEntity

from tests.fixtures.identity import write_mapped
from yizkr.claims import ClaimBatch
from yizkr.pipeline import registrar, writer
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import (
    BatchWriter,
    source_descriptor,
)

from .conftest import fictional_png, fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#431: Nina Sartina, her alias marked with a character the "
    "instance's Unicode tables do not know, and two same-named copies of "
    "her letter"
)
SESSION_MARK = fixture_mark(FIXTURE, writer)

MARK = "\U0001e08f"
DOT_BELOW = "\u0323"
# Nina's surname as the copy writes it, and as the instance's NFC keeps it
MARKED = f"Са{MARK}{DOT_BELOW}ртина"
MARKED_KEPT = f"Са{DOT_BELOW}{MARK}ртина"
FAULT = (
    f"holds U+1E08F, a character Python's Unicode "
    f"{unicodedata.unidata_version} tables do not know, which the instance "
    f"may put in another order in NFC"
)

knows_the_mark = pytest.mark.skipif(
    unicodedata.category(MARK) != "Cn",
    reason=(
        f"this interpreter's Unicode {unicodedata.unidata_version} tables "
        f"assign U+1E08F, so it is no character they do not know"
    ),
)

RUN = {
    "model_identifier": "integration test unknown character fixture model",
    "pipeline_version": "0.0.0",
    "started_at": "2026-10-02T12:00:00Z",
}


def batch_content(sha1: str, prefix: str) -> dict:
    """The fixture's batch, citing the media `sha1`, each entity's label
    after `prefix`."""

    def cites(excerpt: str) -> dict:
        return {
            "artifact": {"kind": "artifact", "sha1": sha1},
            "transcription_excerpt": {"text": excerpt, "language": "ru"},
        }

    nina = {
        "kind": "local",
        "local_id": "nina",
        "label": f"{prefix}Нина Лёбиг",
        "instance_of": "human",
    }
    rudolf = {
        "kind": "local",
        "local_id": "rudolf",
        "label": f"{prefix}Лёбиг Рудольф Феликсович",
        "instance_of": "human",
    }
    return {
        "extraction_run": RUN,
        "claims": [
            {
                "subject": nina,
                "property": "married_name",
                "value": {
                    "type": "monolingual",
                    "value": {"text": "Лёбиг", "language": "ru"},
                },
                "source": cites("по мужу Лёбиг"),
            },
            {
                "subject": nina,
                "property": "relative",
                "value": {"type": "item", "value": rudolf},
                "qualifiers": [
                    {
                        "property": "kinship_type",
                        "value": {
                            "type": "string",
                            "value": f"вдова {MARKED}",
                        },
                    }
                ],
                "source": cites("вдова Рудольфа"),
            },
        ],
        "entities": [
            {
                "id": "nina",
                "label": f"{prefix}Нина Лёбиг",
                "kind": "human",
                "summary": f"fixture: Rudolf Löbig's widow, born {MARKED}",
                "aliases": ["Нина Сартина", f"Нина {MARKED}"],
            }
        ],
    }


def fixture_marker() -> str:
    """The marker (the module docstring)."""
    return hashlib.sha256(
        json.dumps(batch_content("A", ""), sort_keys=True).encode()
        + SESSION_MARK.encode()
    ).hexdigest()[:6]


def prefix_of(marker: str) -> str:
    """What each entity's label begins with: the fixture's name and the
    marker."""
    return f"Unknown Character Fixture ({marker}) "


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch (idempotent: a re-run within the session
    finds and skips everything it writes, and refuses and leaves out the
    same). Returns the source's item, the batch and the write's result."""
    registered = register_fictional(
        client, f"Unknown character fixture source {SESSION_MARK}", "artifact"
    )
    batch = ClaimBatch.model_validate(
        batch_content(registered.ref.sha1, prefix_of(fixture_marker()))
    )
    written = write_mapped(BatchWriter(client), batch)
    return registered.qid, batch, written


def entity(client, qid: str) -> dict:
    """The item as the API gives it."""
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels|descriptions|aliases|claims",
        }
    )["entities"][qid]


def english(item: dict, kind: str):
    """The item's English `kind` terms: a label's or description's value,
    or the aliases' values."""
    terms = item.get(kind, {}).get("en")
    if isinstance(terms, list):
        return [term["value"] for term in terms]
    return terms["value"] if terms else None


def statements(client, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    return entity(client, qid).get("claims", {}).get(properties[key], [])


def cited(client, statement: dict, key: str) -> list[str]:
    properties, _ = client.vocabulary()
    return [
        snak["datavalue"]["value"]["id"]
        for block in statement.get("references", [])
        for snak in block["snaks"].get(properties[key], [])
    ]


@knows_the_mark
def test_what_holds_the_mark_is_refused_or_left_out(client, fixture_graph):
    source, batch, written = fixture_graph
    # the session's first write writes the married name
    assert (written.written, written.referenced, written.skipped) == (1, 0, 0)
    refused = claim_id(batch.claims[1])
    assert written.refused == [refused]
    assert written.refused_terms == ["nina: alias"]
    assert written.warnings == [
        f"claim {refused} (relative) is refused, and nothing of it is "
        f"written: its kinship_type qualifier {FAULT}. A cut or any other "
        f"change would alter what the source says",
        f"entity 'nina' has an alias that {FAULT}: it is left out",
    ]
    assert "rudolf" not in written.created_items
    nina = written.created_items["nina"]
    item = entity(client, nina)
    assert english(item, "labels") == (
        f"{prefix_of(fixture_marker())}Нина Лёбиг"
    )
    assert english(item, "descriptions") == source_descriptor(
        "human", "artifact"
    )
    assert english(item, "aliases") == ["Нина Сартина"]
    assert statements(client, nina, "relative") == []
    (married,) = statements(client, nina, "married_name")
    assert cited(client, married, "stated_in") == [source]
    assert cited(client, married, "inferred_from_extraction_run") == [
        written.run_qid
    ]


@knows_the_mark
def test_a_rewrite_sends_no_edit(client, fixture_graph, monkeypatch):
    _, batch, written = fixture_graph
    nina = written.created_items["nina"]
    before = entity(client, nina)
    edits: list[str | None] = []
    write = BaseEntity._write

    def counted(entity, *args, **kwargs):
        edits.append(entity.id)
        return write(entity, *args, **kwargs)

    monkeypatch.setattr(BaseEntity, "_write", counted)

    again = write_mapped(BatchWriter(client), batch)

    assert edits == []
    assert again.run_qid == written.run_qid
    assert again.created_items == written.created_items
    assert (again.refused, again.refused_terms, again.warnings) == (
        written.refused,
        written.refused_terms,
        written.warnings,
    )
    assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
    assert entity(client, nina) == before


@knows_the_mark
def test_the_instance_keeps_the_mark_in_another_order(client):
    # an item of its own, made once a session with the
    # marked alias in the same write, never edited again; its description
    # keeps its label and description apart from any other item's
    label = f"{prefix_of(fixture_marker())}direct write"
    sent = f"Нина {MARKED}"
    assert unicodedata.normalize("NFC", sent) == sent
    qid = client.find_item_by_label(label)
    if qid is None:
        item = client.wbi.item.new()
        item.labels.set("en", label)
        item.descriptions.set(
            "en", "fixture: an alias written past the writer; safe to delete"
        )
        item.aliases.set("en", [sent])
        qid = item.write(summary="integration test: unknown character").id

    (kept,) = english(entity(client, qid), "aliases")

    # the dot below now comes before the i: the instance's NFC, by Unicode
    # 15.0's tables, is not Python's, by its own
    assert kept == f"Нина {MARKED_KEPT}"
    assert kept != unicodedata.normalize("NFC", sent)


def copies_marker() -> str:
    """The marker of the copies' fixture: over the batch's marker and the
    registrar's source, so each version of the registrar meets the copies
    as a first registration does."""
    return hashlib.sha256(
        fixture_marker().encode() + Path(registrar.__file__).read_bytes()
    ).hexdigest()[:6]


@knows_the_mark
def test_same_named_copies_under_a_marked_title_both_register(
    client, tmp_path, monkeypatch
):
    marker = copies_marker()
    title = f"{prefix_of(marker)}letter of Нина {MARKED}"
    name = f"unknown-character-letter-{marker}.png"
    paths = []
    for copy in ("letters", "copies"):
        path = tmp_path / copy / name
        path.parent.mkdir()
        path.write_bytes(
            fictional_png(
                f"yizkr live test: unknown character {copy} {marker}"
            )
        )
        paths.append(path)
    written: list[str | None] = []
    write = BaseEntity._write

    def counted(entity, *args, **kwargs):
        result = write(entity, *args, **kwargs)
        written.append(result.get("id"))
        return result

    monkeypatch.setattr(BaseEntity, "_write", counted)

    def register_both():
        return [
            register_artifact(
                client,
                media_path=path,
                artifact_type="artifact",
                title=title,
                description="written by tests/integration; safe to delete",
            )
            for path in paths
        ]

    # the session's first registration makes both
    first = register_both()

    qids = [result.artifact_qid for result in first]
    assert len(set(qids)) == 2
    # every write went to one of the two artifacts: no item was left
    # without its label by a write Wikibase refused
    assert set(written) <= set(qids)
    for qid, path in zip(qids, paths, strict=True):
        tag = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
        assert english(entity(client, qid), "labels") == (
            f"{prefix_of(marker)}letter of Нина {MARKED_KEPT} [{tag}]"
        )
    written.clear()

    again = register_both()

    assert [result.artifact_qid for result in again] == qids
    assert written == []
