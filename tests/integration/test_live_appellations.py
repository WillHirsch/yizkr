"""Live check that appellations reach a person's item (ADR-0018 and
ADR-0035 §8, as amended by #499).

The fixture is a letter to Klara, in the universe's terms (docs/EXAMPLES.md):
it writes "Tante Rahel" for her aunt Rahel, whom it also names in full,
and "Omama" for Hildegard. The extraction labels Rahel by the appellation
and gives the forms as `referred_to_as` claims. The batch is assembled by
the chunked extractor itself, from scripted answers, so the label fix and
the alias cleanup run as they do in a scratch run; the writer then writes
it, and the test reads the items back:

* Rahel's item is labelled with her name, and "Tante Rahel" is among its
  aliases; Hildegard's aliases hold "Omama";
* each `referred_to_as` statement carries both references, the artifact
  that states it and the extraction run (CLAUDE.md rules 1 and 2), and
  "Tante Rahel" its `perspective`, Klara;
* written again, the batch writes nothing.

Every label carries the session's marker (conftest.fixture_mark), in the
surname the records give, so each session meets the fixture as a first
write. Like every live test it writes fictional data to the one instance,
so the session that runs it ends by clearing the recorded
instance-contents key (docs/AGENT_PROTOCOL.md §5).
"""

from __future__ import annotations

import json

import pytest

from tests.fixtures.identity import write_mapped
from tests.unit.test_chunking import ScriptedLlm
from yizkr.pipeline import chunking, names, writer
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark, register_fictional

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "ADR-0018 as amended by #499: a letter to Klara writing 'Tante Rahel' "
    "and 'Omama', its appellations aliases and referred_to_as statements"
)
MARK = fixture_mark(FIXTURE, chunking, names, writer)
SURNAME = f"Löbig-{MARK}"
RAHEL = f"Rahel {SURNAME}"
HILDEGARD = f"Hildegard {SURNAME}"
KLARA = f"Klara {SURNAME}"
TEXT = "\n".join(
    [
        f"Liebe {KLARA}!",
        f"Tante Rahel, {RAHEL}, wohnt jetzt Angerring 11.",
        f"Omama, {HILDEGARD}, grüßt Dich.",
    ]
)


def record(given: str) -> dict:
    return {
        "kind": "human",
        "status": "name",
        "given": [{"written": given}],
        "surnames": [{"written": SURNAME}],
    }


def manifest() -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "id": "e1",
                    "label": "Tante Rahel",
                    "aliases": [RAHEL],
                    "kind": "human",
                    "mention_lines": [2],
                    "structure": record("Rahel"),
                },
                {
                    "id": "e2",
                    "label": HILDEGARD,
                    "aliases": ["Omama"],
                    "kind": "human",
                    "mention_lines": [3],
                    "structure": record("Hildegard"),
                },
                {
                    "id": "e3",
                    "label": KLARA,
                    "kind": "human",
                    "mention_lines": [1],
                    "structure": record("Klara"),
                },
            ]
        }
    )


def chunk(source) -> str:
    """The one chunk's answer: each appellation once, Rahel's with Klara
    as its ego, and the literal kinship it holds."""

    def ref(local_id: str) -> dict:
        return {"kind": "local", "local_id": local_id, "instance_of": "human"}

    def cited(excerpt: str) -> dict:
        return {
            "artifact": source.model_dump(mode="json"),
            "page_number": "1",
            "transcription_excerpt": {"text": excerpt, "language": "de"},
        }

    def said(text: str) -> dict:
        return {
            "type": "monolingual",
            "value": {"text": text, "language": "de"},
        }

    claims = [
        {
            "subject": ref("e1"),
            "property": "referred_to_as",
            "value": said("Tante Rahel"),
            "qualifiers": [
                {
                    "property": "perspective",
                    "value": {"type": "item", "value": ref("e3")},
                }
            ],
            "source": cited("Tante Rahel"),
        },
        {
            "subject": ref("e1"),
            "property": "relative",
            "value": {"type": "item", "value": ref("e3")},
            "qualifiers": [
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": "Tante"},
                }
            ],
            "source": cited("Tante Rahel"),
        },
        {
            "subject": ref("e2"),
            "property": "referred_to_as",
            "value": said("Omama"),
            "source": cited("Omama"),
        },
    ]
    return "\n".join([*map(json.dumps, claims), json.dumps({"done": True})])


@pytest.fixture(scope="module")
def written(client):
    """The batch as the extractor assembles it, written; and the result,
    the batch, and the source artifact's QID."""
    registered = register_fictional(
        client, f"Appellation fixture letter {MARK}", "artifact"
    )
    source = registered.ref
    extractor = ChunkedClaimExtractor(
        ScriptedLlm([manifest(), chunk(source)]), budget=10_000
    )
    batch = extractor.extract(
        Transcription(artifact=source, text=TEXT, engine="test"), source
    )
    result = write_mapped(BatchWriter(client), batch)
    return result, batch, registered.qid


def test_the_batch_holds_the_name_and_the_appellation(written):
    """What the writer is given: the label fix and the alias cleanup
    have run where the batch was assembled."""
    _result, batch, _source = written
    by_id = {entity.id: entity for entity in batch.entities}
    assert (by_id["e1"].label, by_id["e1"].aliases) == (RAHEL, ["Tante Rahel"])
    assert by_id["e2"].aliases == ["Omama"]


def test_the_items_show_the_appellation_aliases(client, written):
    result, _batch, _source = written
    rahel = client.wbi.item.get(result.created_items["e1"])
    assert str(rahel.labels.get("en")) == RAHEL
    assert "Tante Rahel" in {str(a) for a in rahel.aliases.get("en") or []}
    hildegard = client.wbi.item.get(result.created_items["e2"])
    assert str(hildegard.labels.get("en")) == HILDEGARD
    assert "Omama" in {str(a) for a in hildegard.aliases.get("en") or []}


def statements(client, qid: str, prop_key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[prop_key],
        }
    )
    return [c for cs in data["claims"].values() for c in cs]


@pytest.mark.parametrize(
    ("local_id", "form"), [("e1", "Tante Rahel"), ("e2", "Omama")]
)
def test_each_appellation_is_a_dual_referenced_statement(
    client, written, local_id, form
):
    result, _batch, source = written
    properties, _ = client.vocabulary()
    (statement,) = statements(
        client, result.created_items[local_id], "referred_to_as"
    )
    assert statement["mainsnak"]["datavalue"]["value"] == {
        "text": form,
        "language": "de",
    }
    assert statement["rank"] == "normal"
    snaks = [ref["snaks"] for ref in statement["references"]]
    stated_in = {
        snak["datavalue"]["value"]["id"]
        for found in snaks
        for snak in found.get(properties["stated_in"], [])
    }
    assert stated_in == {source}
    assert any(properties["inferred_from_extraction_run"] in s for s in snaks)


def test_the_perspective_names_the_ego(client, written):
    result, _batch, _source = written
    properties, _ = client.vocabulary()
    (statement,) = statements(
        client, result.created_items["e1"], "referred_to_as"
    )
    (ego,) = statement["qualifiers"][properties["perspective"]]
    assert ego["datavalue"]["value"]["id"] == result.created_items["e3"]


def test_written_again_it_writes_nothing(client, written):
    result, batch, _source = written
    again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.referenced) == (0, 0)
    assert again.created_items == result.created_items
