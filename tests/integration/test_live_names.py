"""Live write path for name normalization (ADR-0022).

The deterministic alias validator (``pipeline/names.py``) sits on BOTH
ingest paths just before the writer. This proves what lands in the graph:

* LLM path — the chunked extractor's entity manifest is hygiened on its way
  into the batch. A scripted, fictional engine stands in for the model (no
  model call), so the test drives the real extractor wiring end to end.
  Each manifest gives the person the mention record extraction hands over
  (ADR-0035 §2), and the cleanup reads her aliases through it (§8): a form
  that gives no name part the label lacks (a bare given name, an
  initialism, the label in another case) never reaches the item; a form
  that adds a part (a nickname, a birth surname, a hyphenated or particle
  surname, each whole) lands; the source-marked typed names are written
  as referenced statements. A second document contributes to the same
  item through the writer's append path, and only its form that adds a
  part is added. The fixture is tests/fixtures/live_names.py, which an
  offline unit test mirrors.
* GEDCOM path — the adapter's aliases are hygiened the same way, while its
  structured NAME TYPE / NICK / _MARNM tags become typed name statements.

Every document is registered with media of its own, which its claims cite
by SHA-1 (#102). Every name here is fictional.
"""

from __future__ import annotations

import pytest

from tests.fixtures.live_names import (
    EXPECTED_ALIASES,
    FIRST_CLAIMS,
    FIRST_DOCUMENT,
    FIRST_DOCUMENT_ALIASES,
    FIRST_RECORD,
    LABEL,
    SECOND_CLAIMS,
    SECOND_DOCUMENT,
    SECOND_DOCUMENT_ALIASES,
    SECOND_RECORD,
    ScriptedEngine,
    chunk_response,
    entity_manifest,
)
from yizkr.claims import ArtifactRef
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.gedcom import adapt_gedcom
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import BatchWriter

from .conftest import Registered, register_fictional

pytestmark = pytest.mark.live


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
        entity_manifest(FIRST_DOCUMENT_ALIASES, 2, FIRST_RECORD),
        FIRST_CLAIMS,
    )
    # the validator already ran: the batch's manifest holds the clean bag.
    # Checked BEFORE writing: aliases are append-only, so a validator
    # regression must fail here rather than leave the fixture item dirty.
    (entity,) = first.entities
    assert entity.label == LABEL
    assert entity.structure is not None  # the record stood (ADR-0035 §5)
    assert set(entity.aliases) == EXPECTED_ALIASES - {"Ida Testerhof"}
    written = BatchWriter(client).write_batch(first)
    qid = written.created_items["e1"]

    second = extract(
        tmp_path_factory.mktemp("names-second"),
        second_doc,
        SECOND_DOCUMENT,
        entity_manifest(SECOND_DOCUMENT_ALIASES, 2, SECOND_RECORD),
        SECOND_CLAIMS,
    )
    (entity,) = second.entities
    assert entity.structure is not None
    assert set(entity.aliases) == {"Peppi", "Ida Testerhof"}
    again = BatchWriter(client).write_batch(second)
    assert again.created_items["e1"] == qid  # the same item, found by label
    return qid, first_doc, second_doc


def test_llm_path_alias_bag_is_hygiened_in_the_graph(client, llm_path_person):
    qid, _, _ = llm_path_person
    item = client.wbi.item.get(qid)
    assert str(item.labels.get("en")) == LABEL
    # exactly the forms that add a part to the label, each whole; nothing
    # derivable leaked in, and no compound was split into particle tokens
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
        entity_manifest(FIRST_DOCUMENT_ALIASES, 2, FIRST_RECORD),
        FIRST_CLAIMS,
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
