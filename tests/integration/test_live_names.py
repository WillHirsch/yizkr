"""Live write path for name normalization (ADR-0022).

The deterministic alias validator (``pipeline/names.py``) sits on BOTH
ingest paths just before the writer. This proves what lands in the graph:

* LLM path — the chunked extractor's entity manifest is hygiened on its way
  into the batch. A scripted, fictional engine stands in for the model (no
  model call), so the test drives the real extractor wiring end to end:
  derivable short-forms (a bare given name, an initialism, a case-variant of
  the label) never reach the item; a nickname, a licensed alternative-surname
  composition, and hyphenated / particle compound surnames land WHOLE; the
  source-marked typed names are written as referenced statements. A second
  document contributes to the same item through the writer's append path,
  and only its genuinely new form is added.
* GEDCOM path — the adapter's aliases are hygiened the same way, while its
  structured NAME TYPE / NICK / _MARNM tags become typed name statements.

Every document is registered with media of its own, which its claims cite
by SHA-1 (#102). Every name here is fictional.
"""

from __future__ import annotations

import json

import pytest

from yizkr.claims import ArtifactRef
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import BatchWriter

from .conftest import Registered, register_fictional

pytestmark = pytest.mark.live

LABEL = "Ida Livecheck"
# the forms a model might list for one person, messy on purpose
FIRST_DOCUMENT_ALIASES = [
    "Ida",  # bare given name: derivable from the label -> dropped
    "I. Livecheck",  # initialism: derivable -> dropped
    "ida livecheck",  # the label again, other case -> dropped
    "Peppi",  # nickname: adds a token -> kept
    "PEPPI",  # case-duplicate of a kept form -> dropped
    "Ida Probewald",  # licensed alternative-surname composition -> kept
    "Livecheck-Probewald",  # hyphenated compound: kept whole
    "van der Probewald",  # particle surname: kept whole, never split
    "  ",  # blank -> dropped
]
SECOND_DOCUMENT_ALIASES = ["Ida", "Peppi", "I. Livecheck", "Ida Testerhof"]
EXPECTED_ALIASES = {
    "Peppi",
    "Ida Probewald",
    "Livecheck-Probewald",
    "van der Probewald",
    "Ida Testerhof",  # the second document's one genuinely new form
}

FIRST_DOCUMENT = """\
Register of the Livecheck household, 1950.
1. Ida Livecheck, nee Probewald, called Peppi; b. 1881 in Moshausen.
"""
SECOND_DOCUMENT = """\
Letter from Stupfield, 1960.
Mrs Ida Testerhof (her married name) wrote; the family called her Peppi.
"""


class ScriptedEngine:
    """A fictional stand-in for the model: fixed responses, no model call.
    It has no conversation support, so the extractor sends chunks inline."""

    model_identifier = "integration-test-scripted"

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def complete(self, prompt, image_paths=()):
        return self.responses.pop(0)


def entity_manifest(aliases: list[str], mention_line: int) -> str:
    return json.dumps(
        {
            "entities": [
                {
                    "id": "e1",
                    "label": LABEL,
                    "aliases": aliases,
                    "kind": "human",
                    "summary": "fictional live-test person",
                    "mention_lines": [mention_line],
                }
            ]
        }
    )


def chunk_response(artifact: ArtifactRef, claims: list[tuple]) -> str:
    """A chunk's answer, each claim citing `artifact` as the prompt asks."""
    lines = [
        json.dumps(
            {
                "subject": {
                    "kind": "local",
                    "local_id": "e1",
                    "label": LABEL,
                    "instance_of": "human",
                },
                "property": prop,
                "value": value,
                "source": {
                    "artifact": artifact.model_dump(mode="json"),
                    "transcription_excerpt": {
                        "text": excerpt,
                        "language": "en",
                    },
                },
                "confidence": 0.9,
            }
        )
        for prop, value, excerpt in claims
    ]
    lines.append(json.dumps({"done": True, "claims_emitted": len(claims)}))
    return "\n".join(lines)


def name(text: str) -> dict:
    return {"type": "monolingual", "value": {"text": text, "language": "en"}}


def fixture_artifact(client, title: str) -> Registered:
    return register_fictional(client, title, "letter")


def extract(tmp_path, artifact: Registered, document, manifest, claims):
    engine = ScriptedEngine([manifest, chunk_response(artifact.ref, claims)])
    extractor = ChunkedClaimExtractor(
        engine, state_dir=tmp_path, budget=10_000
    )
    batch = extractor.extract(
        Transcription(artifact=artifact.ref, text=document, engine="test"),
        artifact.ref,
    )
    assert all(claim.source.artifact == artifact.ref for claim in batch.claims)
    assert not extractor.used_fallback_manifest  # the entity-first path ran
    assert extractor.failed_chunks == []
    return batch


def aliases_on(client, qid: str) -> set[str]:
    item = client.wbi.item.get(qid)
    return {str(alias) for alias in (item.aliases.get("en") or [])}


def typed_names(client, qid: str, prop_key: str) -> dict[str, set[str]]:
    """{name text -> stated-in artifact QIDs}, asserting each statement
    carries both the source and the extraction-run reference."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties[prop_key],
        }
    )
    found: dict[str, set[str]] = {}
    for claim in (c for cs in data["claims"].values() for c in cs):
        references = [ref["snaks"] for ref in claim["references"]]
        run_pid = properties["inferred_from_extraction_run"]
        assert any(run_pid in snaks for snaks in references)
        text = claim["mainsnak"]["datavalue"]["value"]["text"]
        found.setdefault(text, set()).update(
            snak["datavalue"]["value"]["id"]
            for snaks in references
            for snak in snaks.get(properties["stated_in"], [])
        )
    return found


def stated_in(names: dict[str, set[str]], artifact_qid: str) -> set[str]:
    """The name texts of `names` whose statements `artifact_qid` states.
    An earlier run's export, registered as an artifact of its own when
    its text differed, may state the same name on the same item: the
    two sources' statements stand side by side."""
    return {text for text, cited in names.items() if artifact_qid in cited}


@pytest.fixture(scope="module")
def llm_path_person(client, tmp_path_factory):
    """Two fictional documents about one person, written through the real
    extractor wiring and writer. Returns (item QID, doc 1, doc 2)."""
    first_doc = fixture_artifact(client, "Integration names register")
    second_doc = fixture_artifact(client, "Integration names letter")

    first = extract(
        tmp_path_factory.mktemp("names-first"),
        first_doc,
        FIRST_DOCUMENT,
        entity_manifest(FIRST_DOCUMENT_ALIASES, mention_line=2),
        [
            ("birth_name", name("Ida Probewald"), "nee Probewald"),
            ("nickname", name("Peppi"), "called Peppi"),
            ("date_of_birth", {"type": "time", "year": 1881}, "b. 1881"),
        ],
    )
    # the validator already ran: the batch's manifest holds the clean bag.
    # Checked BEFORE writing: aliases are append-only, so a validator
    # regression must fail here rather than leave the fixture item dirty.
    (entity,) = first.entities
    assert entity.label == LABEL
    assert set(entity.aliases) == EXPECTED_ALIASES - {"Ida Testerhof"}
    written = BatchWriter(client).write_batch(first)
    qid = written.created_items["e1"]

    second = extract(
        tmp_path_factory.mktemp("names-second"),
        second_doc,
        SECOND_DOCUMENT,
        entity_manifest(SECOND_DOCUMENT_ALIASES, mention_line=2),
        [("married_name", name("Ida Testerhof"), "Mrs Ida Testerhof")],
    )
    again = BatchWriter(client).write_batch(second)
    assert again.created_items["e1"] == qid  # the same item, found by label
    return qid, first_doc, second_doc


def test_llm_path_alias_bag_is_hygiened_in_the_graph(client, llm_path_person):
    qid, _, _ = llm_path_person
    item = client.wbi.item.get(qid)
    assert str(item.labels.get("en")) == LABEL
    # exactly the non-derivable forms, each whole; nothing derivable leaked
    # in, and no compound was split into particle tokens
    assert aliases_on(client, qid) == EXPECTED_ALIASES


def test_llm_path_typed_names_are_referenced_statements(
    client, llm_path_person
):
    qid, first_doc, second_doc = llm_path_person
    assert typed_names(client, qid, "birth_name") == {
        "Ida Probewald": {first_doc.qid}
    }
    assert typed_names(client, qid, "nickname") == {"Peppi": {first_doc.qid}}
    assert typed_names(client, qid, "married_name") == {
        "Ida Testerhof": {second_doc.qid}
    }


def test_llm_path_rewrite_changes_nothing(client, llm_path_person, tmp_path):
    """Idempotent: re-extracting and re-writing the first document adds no
    statement and no alias."""
    qid, first_doc, _ = llm_path_person
    batch = extract(
        tmp_path,
        first_doc,
        FIRST_DOCUMENT,
        entity_manifest(FIRST_DOCUMENT_ALIASES, mention_line=2),
        [
            ("birth_name", name("Ida Probewald"), "nee Probewald"),
            ("nickname", name("Peppi"), "called Peppi"),
            ("date_of_birth", {"type": "time", "year": 1881}, "b. 1881"),
        ],
    )
    result = BatchWriter(client).write_batch(batch)
    assert result.written == 0
    assert aliases_on(client, qid) == EXPECTED_ALIASES


GEDCOM = """\
0 HEAD
1 CHAR UTF-8
0 @I1@ INDI
1 NAME Hildegard Rahel /Namecheck/
2 TYPE birth
2 NICK Hilde
1 NAME Hildegard /Namecheck/
1 NAME Hildegard /Probewald-Namecheck/
2 TYPE married
1 NAME Hildegard /van der Probewald/
1 _MARNM Hildegard /Probewald/
1 BIRT
2 DATE 1876
0 TRLR
"""


def test_gedcom_path_alias_bag_and_typed_names(client, tmp_path):
    """The adapter's names are hygiened like the LLM path's: the derivable
    'Hildegard Namecheck' is dropped, the compound forms land whole, a
    NICK and a `_MARNM` are name forms too (ADR-0035 §6, #408), and the
    structured tags become typed, referenced name statements."""
    export = tmp_path / "integration-names-tree.ged"
    export.write_text(GEDCOM)
    exported = ArtifactRef.from_path(export)
    artifact_qid = register_artifact(
        client,
        media_path=export,
        artifact_type="genealogical_report",
        title="Integration names GEDCOM tree",
        description="written by tests/integration; safe to delete",
    ).artifact_qid
    expected_aliases = {
        "Hildegard Probewald-Namecheck",
        "Hildegard van der Probewald",
        "Hilde",
        "Hildegard Probewald",
    }
    result = adapt_gedcom(GEDCOM, artifact=exported)
    # checked BEFORE writing: aliases are append-only, so a validator
    # regression must fail here rather than leave the fixture item dirty
    (person,) = [e for e in result.batch.entities if e.id == "i1"]
    assert set(person.aliases) == expected_aliases
    qid = BatchWriter(client).write_batch(result.batch).created_items["i1"]

    item = client.wbi.item.get(qid)
    assert str(item.labels.get("en")) == "Hildegard Rahel Namecheck"
    assert aliases_on(client, qid) == expected_aliases
    assert stated_in(typed_names(client, qid, "birth_name"), artifact_qid) == {
        "Hildegard Rahel Namecheck"
    }
    assert stated_in(typed_names(client, qid, "nickname"), artifact_qid) == {
        "Hilde"
    }
    assert stated_in(
        typed_names(client, qid, "married_name"), artifact_qid
    ) == {"Hildegard Probewald-Namecheck", "Hildegard Probewald"}

    rerun = BatchWriter(client).write_batch(
        adapt_gedcom(GEDCOM, artifact=exported).batch
    )
    assert rerun.written == 0
