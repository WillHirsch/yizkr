"""Live validation of the shape of the text the writer sends (#384; the
writer's LINE_BREAKS, WHITESPACE, `_locator_text` and `_term_text`).

Wikibase refuses a text value that holds a line break or a tab, or has
whitespace at either end (its `illegal-string-chars` check,
/^\\s|[\\v\\t]|\\s$/u, which PHP matches by Unicode property), and a label,
description or alias of more than 250 characters once it has trimmed it,
and it refuses the whole edit that sends one. A fictional fixture writes
one batch of eight claims about Eugen Löbig and his daughter-in-law
Hildegard, as (here) the 1909 burial register gives them, and reads them
back through the API:

* the first gives his cause of death over two lines, the third his age at
  death with a tab in it, the seventh Hildegard's kinship to him ending in
  a line separator (U+2028), which WikibaseIntegrator lets through, and the
  eighth his nickname ending in a no-break space: the writer refuses all
  four, names them in the result by their review-sheet ids and says why,
  and writes nothing of them, so no edit of the batch is refused;
* the second gives his date of death on a page and with an excerpt that
  hold carriage returns, line feeds and tabs, and whitespace at either
  end: the instance keeps both on one line, each line break a visible
  " | ";
* the fourth and fifth give the date again, each with an excerpt over the
  limit, the two agreeing well past the cut: each is cut at a break
  between words and ends with the digest of the whole, so the statement
  holds a block for each;
* Eugen's label, his summary and two of his aliases are over the 250
  characters Wikibase holds in a term, and his other aliases are his pet
  name with a line break after it, with spaces around it, and a blank: the
  instance keeps the label, the description and the two aliases cut, with
  their digests, the pet name once, and no blank. Hildegard's label, given
  with a line break before it, a tab inside and a space after it, agrees
  with Eugen's well past the cut: she has an item of her own, whose label
  is hers trimmed and cut;
* the batch's model identifier makes the run item's label longer than the
  instance holds: it is cut with the digest of the whole label, which
  holds the batch's fingerprint;
* writing the batch again sends no edit at all, and refuses the same four
  claims.

Every entity's label carries a marker: six hex digits of a hash over the
fixture, its cited SHA-1 replaced by "A", and over the writer's source, as
test_live_text_limits.py's labels do, so each version of the fixture and
of the writer meets the fixture as a first write does. Each entity has a
description, so a writer that failed to find one by its label could not
mint a second item under it. The source is registered with a fictional
PNG of its own (register_fictional), which the claims cite by SHA-1
(#102). Like every live test it writes fictional data to the one
instance, so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
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
from yizkr.pipeline.writer import CUT_MARK, BatchWriter, batch_fingerprint

from .conftest import register_fictional

pytestmark = pytest.mark.live

# the most characters Wikibase holds in a text value, and in a term
TEXT_LIMIT, TERM_LIMIT = 400, 250
LINE_SEPARATOR = "\N{LINE SEPARATOR}"
NO_BREAK_SPACE = "\xa0"


def said_over(phrase: str, length: int) -> str:
    """`phrase` said over and over, `length` characters long in NFC, the
    last of them a full stop."""
    text = (phrase * (length // len(phrase) + 1))[: length - 1] + "."
    assert len(unicodedata.normalize("NFC", text)) == length
    return text


def mark(text: str) -> str:
    """What a text over its limit is cut with: CUT_MARK, then "sha1" and
    the first 12 hex digits of the SHA-1 of the whole text in NFC."""
    whole = unicodedata.normalize("NFC", text).encode()
    return f"{CUT_MARK} sha1 {hashlib.sha1(whole).hexdigest()[:12]}"


def room(limit: int) -> int:
    """The room a cut to `limit` leaves before its mark."""
    return limit - len(mark(""))


def cut(text: str, limit: int) -> str:
    """`text`, over `limit` and its words each after a space, cut as the
    writer cuts it: at the last space the room holds or ends at, its mark
    after it."""
    return text[: text.rindex(" ", 0, room(limit) + 1)].rstrip() + mark(text)


# (here) the heading of the register's index, which an extraction takes
# into the labels of the people the index names and into Eugen's aliases:
# they agree well past the room
INDEX = said_over(
    "Beerdigte der Gemeinde Flörburg im Jahre 1909, Blatt 12, ", 240
)
SUMMARY = said_over(
    "Master hatter of Flörburg, founder of the Hutmacherei zum Blauen Hut "
    "in 1863; ",
    300,
)
assert SUMMARY[room(TERM_LIMIT) - 3 : room(TERM_LIMIT) + 2] == " 1863"
LONG_ALIASES = [
    f"{INDEX} Muckel, Hutmachermeister",
    f"{INDEX} Eugen 'Muckel' Löbig",
]
for alias in LONG_ALIASES:
    assert alias[room(TERM_LIMIT) - 2 : room(TERM_LIMIT) + 1] == "Bee"
ENTITIES = (
    {
        "id": "eugen",
        "label": "Eugen Löbig",
        "kind": "human",
        "summary": SUMMARY,
        "aliases": ["Muckel\n", " Muckel ", " \t", *LONG_ALIASES],
    },
    {
        "id": "hildegard",
        "label": "Hildegard Löbig",
        "kind": "human",
        "summary": "fixture: Hildegard Löbig, Eugen's daughter-in-law",
    },
)
# the page and excerpt of his date of death, over several lines, and as
# the instance keeps them
PAGE = "\r\n Blatt 12,\r\nFlörburg \t"
PAGE_KEPT = "Blatt 12, | Flörburg"
EXCERPT = "\tGestorben:\r16. März\t1909\nin Flörburg.\n"
EXCERPT_KEPT = "Gestorben: | 16. März 1909 | in Flörburg."
# (here) the register's entry quoted at length, twice, as it is copied
# onto two leaves: the room ends inside "Gestorben" in both
LONG_EXCERPTS = [
    said_over("Gestorben: 16. März 1909 in Flörburg, Brunnenring 35; ", 420)
    + f" Blatt {leaf}"
    for leaf in (12, 13)
]
for excerpt in LONG_EXCERPTS:
    assert excerpt[room(TEXT_LIMIT) - 2 : room(TEXT_LIMIT) + 1] == "Ges"
# (here) the model's identifier names its whole setup
MODEL = said_over("integration test shape fixture model, ", 240)
RUN = {
    "model_identifier": MODEL,
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-29T12:00:00Z",
}


def labels_of(marker: str) -> dict[str, str]:
    """Each entity's label as the fixture's refs and records give it, the
    marker after the fixture's name, then the index's heading: Hildegard's
    with a line break before it, a tab inside and a space after it."""
    return {
        "eugen": f"Text Shape Fixture ({marker}) {INDEX} Eugen Löbig",
        "hildegard": (
            f"\nText Shape Fixture ({marker}) {INDEX}\tHildegard Löbig "
        ),
    }


def label_kept(marker: str, key: str) -> str:
    """The entity's label as the instance keeps it: trimmed, its tab a
    space, and cut to the limit, with the digest of the label trimmed."""
    trimmed = " ".join(labels_of(marker)[key].split())
    assert trimmed[room(TERM_LIMIT) - 1 : room(TERM_LIMIT) + 2] == "g i"
    return cut(trimmed, TERM_LIMIT)


def batch_content(sha1: str, labels: dict[str, str]) -> dict:
    """The fixture's batch, citing the media `sha1`, its entities under
    `labels`."""

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

    def of_eugen(key: str, value: dict, excerpt: str) -> dict:
        return {
            "subject": ref("eugen"),
            "property": key,
            "value": value,
            "source": cites(excerpt),
        }

    def text(words: str) -> dict:
        return {
            "type": "monolingual",
            "value": {"text": words, "language": "de"},
        }

    def died(excerpt: str) -> dict:
        return {
            "subject": ref("eugen"),
            "property": "date_of_death",
            "value": {"type": "time", "year": 1909, "month": 3, "day": 16},
            "source": cites(excerpt, PAGE),
        }

    def kinship(term: str) -> dict:
        return {
            "subject": ref("hildegard"),
            "property": "relative",
            "value": {"type": "item", "value": ref("eugen")},
            "qualifiers": [
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": term},
                }
            ],
            "source": cites("Angezeigt von der Schwiegertochter."),
        }

    return {
        "extraction_run": RUN,
        "claims": [
            of_eugen(
                "cause_of_death",
                text("Wassersucht,\nHerzschwäche"),
                "Todesursache: Wassersucht, Herzschwäche.",
            ),
            died(EXCERPT),
            of_eugen(
                "age_at_death",
                {"type": "string", "value": "73\tJahre"},
                "Alter: 73 Jahre.",
            ),
            died(LONG_EXCERPTS[0]),
            died(LONG_EXCERPTS[1]),
            kinship("Schwiegertochter"),
            kinship(f"Schwiegertochter{LINE_SEPARATOR}"),
            of_eugen(
                "nickname",
                text(f"Muckel{NO_BREAK_SPACE}"),
                "Eugen Löbig, genannt Muckel.",
            ),
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


def fixture_batch(artifact: ArtifactRef) -> ClaimBatch:
    """The fixture's batch under labels that carry the marker."""
    content = batch_content(artifact.sha1, labels_of(fixture_marker()))
    return ClaimBatch.model_validate(content)


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch (idempotent: a re-run of this version
    finds and skips everything it writes, and refuses the same claims).
    Returns the source's item, the batch and the write's result."""
    registered = register_fictional(
        client, "Text shape fixture source", "artifact"
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


def terms(client, qids: list[str]) -> dict[str, dict]:
    """Each item's English label, description and aliases, as the API
    gives them."""
    found = client.api_get(
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels|descriptions|aliases",
            "languages": "en",
        }
    )["entities"]
    return {
        qid: {
            "label": entity.get("labels", {}).get("en", {}).get("value"),
            "description": entity.get("descriptions", {})
            .get("en", {})
            .get("value"),
            "aliases": sorted(
                alias["value"]
                for alias in entity.get("aliases", {}).get("en", [])
            ),
        }
        for qid, entity in found.items()
    }


def test_no_text_of_the_batch_stops_it(client, fixture_graph):
    source, batch, written = fixture_graph
    # the first write of this version writes the date of death and the
    # kinship, and records the long excerpts on the date's statement; a
    # later session's write finds all four there
    assert (written.written, written.referenced, written.skipped) in (
        (2, 2, 0),
        (0, 0, 4),
    )
    refused = [claim_id(batch.claims[index]) for index in (0, 2, 6, 7)]
    assert written.refused == refused
    faults = [
        "(cause_of_death) is refused, and nothing of it is written: its "
        "value holds a line break,",
        "(age_at_death) is refused, and nothing of it is written: its value "
        "holds a tab,",
        "(relative) is refused, and nothing of it is written: its "
        "kinship_type qualifier holds a line break and whitespace at its "
        "end,",
        "(nickname) is refused, and nothing of it is written: its value "
        "holds whitespace at its end,",
    ]
    for warning, cid, fault in zip(
        written.warnings, refused, faults, strict=True
    ):
        assert warning.startswith(f"claim {cid} {fault}")
    eugen = written.created_items["eugen"]
    hildegard = written.created_items["hildegard"]
    assert eugen != hildegard
    # nothing of the refused claims is on the instance
    for key in ("cause_of_death", "age_at_death", "nickname"):
        assert statements(client, eugen, key) == []
    (relative,) = statements(client, hildegard, "relative")
    properties, _ = client.vocabulary()
    assert [
        snak["datavalue"]["value"]
        for snak in relative["qualifiers"][properties["kinship_type"]]
    ] == ["Schwiegertochter"]
    # the date of death: a block for each place, its page and excerpt on
    # one line, the long excerpts cut apart by their digests, and the run
    # once (rules 1 and 2)
    (died,) = statements(client, eugen, "date_of_death")
    assert cited(client, died, "page_number") == [PAGE_KEPT] * 3
    assert cited(client, died, "transcription_excerpt") == [
        EXCERPT_KEPT,
        *(cut(excerpt, TEXT_LIMIT) for excerpt in LONG_EXCERPTS),
    ]
    assert cited(client, died, "stated_in") == [source] * 3
    assert cited(client, died, "inferred_from_extraction_run") == [
        written.run_qid
    ]
    # the terms, as the instance keeps them
    marker = fixture_marker()
    kept = terms(client, [eugen, hildegard, written.run_qid])
    assert kept[eugen] == {
        "label": label_kept(marker, "eugen"),
        "description": cut(SUMMARY, TERM_LIMIT),
        "aliases": sorted(
            ["Muckel", *(cut(alias, TERM_LIMIT) for alias in LONG_ALIASES)]
        ),
    }
    assert kept[hildegard]["label"] == label_kept(marker, "hildegard")
    assert kept[eugen]["label"] != kept[hildegard]["label"]
    run_label = f"extraction run 2026-09-29 {MODEL} {batch_fingerprint(batch)}"
    # the room ends inside "test", which the cut leaves out whole
    assert run_label[room(TERM_LIMIT) - 3 : room(TERM_LIMIT) + 2] == " test"
    assert kept[written.run_qid]["label"] == cut(run_label, TERM_LIMIT)
    for term in (*kept[eugen].values(), kept[hildegard]["label"]):
        for text in term if isinstance(term, list) else [term]:
            assert len(text) <= TERM_LIMIT


def test_a_rewrite_sends_no_edit(client, fixture_graph, monkeypatch):
    _, batch, written = fixture_graph
    qids = [
        written.created_items["eugen"],
        written.created_items["hildegard"],
        written.run_qid,
    ]
    before = (
        terms(client, qids),
        [
            statements(client, qids[0], "date_of_death"),
            statements(client, qids[1], "relative"),
        ],
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
    assert (again.written, again.referenced, again.skipped) == (0, 0, 4)
    assert (
        terms(client, qids),
        [
            statements(client, qids[0], "date_of_death"),
            statements(client, qids[1], "relative"),
        ],
    ) == before
