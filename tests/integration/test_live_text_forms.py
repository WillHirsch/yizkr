"""Live validation of a batch whose text is decomposed (#349; the
writer's `_stored_string`).

Wikibase saves string and external-id values, labels, descriptions and
aliases in Unicode NFC, and its API gives every text back in NFC,
monolingual text included. A fictional fixture gives every text of its
batch in the decomposed form (NFD), as a GEDCOM export can: its items'
labels, an alias and a description, a monolingual value, a string
qualifier, an external id, the excerpts, and the model identifier its run
item is named by. It is written, and read back:

* the items and statements hold the fixture's text in NFC. Wikibase
  keeps a monolingual text as it is sent, but its API gives every text
  back in NFC, and the query service takes canonically equivalent
  literals as one term, so only the entity's RDF, which
  Special:EntityData serves from the stored revision, shows the form
  the birth name is stored in: NFC, the form the writer sent it in;
* writing the batch again sends no edit at all: the writer finds each
  item by its label, each alias on its item, each statement with its
  source block, and the run item by its label;
* the same batch given in NFC finds the same items and statements, and
  adds nothing to them.

The source is registered with a fictional PNG of its own
(register_fictional), which the claims cite by SHA-1 (#102). Its title and
the labels carry the session's marker (conftest.fixture_mark), so each
session meets the fixture as a first write. Like every live test it writes
fictional data to the one instance, so the session that runs it ends by
clearing the recorded instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest
import requests
from wikibaseintegrator.entities.baseentity import BaseEntity

from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.core.config import USER_AGENT
from yizkr.pipeline import writer
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#349: Ida, her sister-in-law Hildegard and Flörburg, every text of "
    "the batch given decomposed, then composed"
)
MARK = fixture_mark(FIXTURE, writer)

# the fixture's entities as its source names them, before the marker: Ida,
# by her married name with her birth name as an alias (ADR-0011), and
# (here) her sister-in-law Hildegard and the town, Flörburg. Each has a
# description: Wikibase refuses an item whose label and description are
# another item's, so a writer that failed to find one of them by its label
# could not mint a second item under the same label, which would stop
# every writer's label index on the instance
ENTITIES = (
    {
        "id": "ida",
        "label": "Text Form Fixture Ida Löbig",
        "aliases": ["Ida Sörne"],
        "kind": "human",
        "summary": "fixture: Ida Löbig née Sörne, of Flörburg",
    },
    {
        "id": "hildegard",
        "label": "Text Form Fixture Hildegard Löbig",
        "kind": "human",
        "summary": "fixture: Hildegard Löbig, Ida's sister-in-law",
    },
    {
        "id": "town",
        "label": "Text Form Fixture Flörburg",
        "kind": "place",
        "summary": "fixture: the town of Flörburg",
    },
)
KEYS = [entity["id"] for entity in ENTITIES]
BIRTH_NAME = "Ida Sörne"
KINSHIP = "Schwägerin"
IDENTIFIER = "flörburg"
RUN = {
    # a model identifier is free text: (here) a local model's
    "model_identifier": "integration-test für Kurrent",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-29T09:00:00Z",
}


def in_form(value, form: str):
    """`value`, a JSON document, with every string in it in the Unicode
    normalization form `form`."""
    if isinstance(value, str):
        return unicodedata.normalize(form, value)
    if isinstance(value, dict):
        return {key: in_form(item, form) for key, item in value.items()}
    if isinstance(value, list):
        return [in_form(item, form) for item in value]
    return value


def batch_content(sha1: str, labels: dict[str, str]) -> dict:
    """The fixture's batch in NFC, citing the media `sha1`, its entities
    under `labels`."""
    kinds = {entity["id"]: entity["kind"] for entity in ENTITIES}

    def ref(key: str) -> dict:
        return {
            "kind": "local",
            "local_id": key,
            "label": labels[key],
            "instance_of": kinds[key],
        }

    def cites(excerpt: str | None = None, language: str = "de") -> dict:
        source = {"artifact": {"kind": "artifact", "sha1": sha1}}
        if excerpt:
            source["transcription_excerpt"] = {
                "text": excerpt,
                "language": language,
            }
        return source

    return {
        "extraction_run": RUN,
        "claims": [
            {
                "subject": ref("ida"),
                "property": "birth_name",
                "value": {
                    "type": "monolingual",
                    "value": {"text": BIRTH_NAME, "language": "de"},
                },
                "source": cites("LÖBIG, Ida, née SÖRNE", "en"),
            },
            {
                "subject": ref("hildegard"),
                "property": "relative",
                "value": {"type": "item", "value": ref("ida")},
                "qualifiers": [
                    {
                        "property": "kinship_type",
                        "value": {"type": "string", "value": KINSHIP},
                    }
                ],
                "source": cites(
                    "Hildegard Löbig, Schwägerin der Verstorbenen"
                ),
            },
            {
                # (here) a gazetteer that keys the town by its name
                "subject": ref("town"),
                "property": "whg_place_id",
                "value": {"type": "external-id", "value": IDENTIFIER},
                "source": cites(),
            },
        ],
        "entities": [
            dict(entity, label=labels[entity["id"]]) for entity in ENTITIES
        ],
    }


def fixture_batch(artifact: ArtifactRef, form: str) -> ClaimBatch:
    """The fixture's batch with every text in the normalization form
    `form`, under labels that carry a marker: six hex digits of a hash
    over the fixture in NFC, its cited SHA-1 replaced by "A", and over the
    module's marker (conftest.fixture_mark: the writer's source and the
    session's random bytes), as test_live_locators.py's fixture_batch
    marks its person's label. The writer finds a local item by its label,
    so each session, and each version of the fixture and of the writer,
    meets the fixture as a first write does: one that finds no item by a
    label given in NFD (a mutant of the harness, say) cannot pass by
    finding an item an earlier session wrote, and what a live mutant's run
    wrote never meets a later session. A re-run within the session, in
    either form, finds its own items again."""
    plain = {entity["id"]: entity["label"] for entity in ENTITIES}
    marker = hashlib.sha256(
        json.dumps(batch_content("A", plain), sort_keys=True).encode()
        + MARK.encode()
    ).hexdigest()[:6]
    labels = {key: f"{label} ({marker})" for key, label in plain.items()}
    return ClaimBatch.model_validate(
        in_form(batch_content(artifact.sha1, labels), form)
    )


@pytest.fixture(scope="module")
def fixture_graph(client):
    """Write the fixture's batch, its text in NFD (idempotent: a re-run
    within the session finds and skips everything). Returns the source's
    registration and the write's result."""
    registered = register_fictional(
        client, f"Text form fixture source {MARK}", "certificate"
    )
    batch = fixture_batch(registered.ref, "NFD")
    assert fixture_batch(registered.ref, "NFC") != batch
    written = BatchWriter(client).write_batch(batch)
    return registered, written


def items(client, written) -> dict[str, dict]:
    """The fixture's items as the API gives them, with their revisions,
    by entity id."""
    qids = {key: written.created_items[key] for key in KEYS}
    found = client.api_get(
        {
            "action": "wbgetentities",
            "ids": "|".join(qids.values()),
            "props": "info|labels|aliases|descriptions|claims",
        }
    )["entities"]
    return {key: found[qid] for key, qid in qids.items()}


def test_the_fixture_is_kept_in_nfc(client, config, fixture_graph):
    registered, written = fixture_graph
    # the session's first write writes the three claims
    assert (written.written, written.referenced, written.skipped) == (3, 0, 0)
    properties, _ = client.vocabulary()
    found = items(client, written)
    for record in fixture_batch(registered.ref, "NFC").entities:
        item = found[record.id]
        assert item["labels"]["en"]["value"] == record.label
        assert [
            alias["value"] for alias in item.get("aliases", {}).get("en", [])
        ] == record.aliases
        description = item.get("descriptions", {}).get("en", {})
        assert description.get("value") == (record.summary or None)

    def values(key: str, prop: str) -> list:
        return [
            statement["mainsnak"]["datavalue"]["value"]
            for statement in found[key]["claims"][properties[prop]]
        ]

    nfc = {
        text: unicodedata.normalize("NFC", text)
        for text in (BIRTH_NAME, KINSHIP, IDENTIFIER)
    }
    assert values("ida", "birth_name") == [
        {"text": nfc[BIRTH_NAME], "language": "de"}
    ]
    (relative,) = found["hildegard"]["claims"][properties["relative"]]
    assert [
        snak["datavalue"]["value"]
        for snak in relative["qualifiers"][properties["kinship_type"]]
    ] == [nfc[KINSHIP]]
    assert values("town", "whg_place_id") == [nfc[IDENTIFIER]]
    # the API gives every text back in NFC, and the query service takes
    # canonically equivalent literals as one term, so only the entity's
    # RDF, served from its stored revision as it is, shows the form the
    # birth name is stored in, which is the form the writer sent it in
    ida = written.created_items["ida"]
    response = requests.get(
        f"{config.wikibase_url}/wiki/Special:EntityData/{ida}.ttl",
        params={"flavor": "dump"},
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    response.raise_for_status()
    rdf = response.content.decode("utf-8")
    decomposed = unicodedata.normalize("NFD", BIRTH_NAME)
    assert f'"{nfc[BIRTH_NAME]}"@de' in rdf
    assert f'"{decomposed}"@de' not in rdf


def test_a_rewrite_sends_no_edit(client, fixture_graph, monkeypatch):
    registered, written = fixture_graph
    before = items(client, written)
    edits: list[str | None] = []
    write = BaseEntity._write

    def counted(entity, *args, **kwargs):
        edits.append(entity.id)
        return write(entity, *args, **kwargs)

    monkeypatch.setattr(BaseEntity, "_write", counted)

    again = BatchWriter(client).write_batch(
        fixture_batch(registered.ref, "NFD")
    )

    assert edits == []
    assert again.run_qid == written.run_qid
    assert [again.created_items[key] for key in KEYS] == [
        written.created_items[key] for key in KEYS
    ]
    assert (again.written, again.referenced, again.skipped) == (0, 0, 3)
    assert items(client, written) == before


def test_the_batch_in_nfc_adds_nothing(client, fixture_graph):
    registered, written = fixture_graph
    before = items(client, written)

    again = BatchWriter(client).write_batch(
        fixture_batch(registered.ref, "NFC")
    )

    assert [again.created_items[key] for key in KEYS] == [
        written.created_items[key] for key in KEYS
    ]
    assert (again.written, again.referenced, again.skipped) == (0, 0, 3)
    assert items(client, written) == before
