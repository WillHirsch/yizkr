"""The live names test's LLM-path fixture (tests/fixtures/live_names.py),
run offline through the real extractor wiring, so that the live test's
expectations cannot drift from the code without a unit test failing.

Each document's manifest gives the person the mention record extraction
hands over (ADR-0035 §2); the record stands (§5), and the alias cleanup
reads her aliases through it (§8). A person without a record keeps her
aliases, blanks and duplicates aside. No wiki, no model.
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
    entity,
    entity_manifest,
)
from yizkr.claims import (
    ArtifactRef,
    EntityRecord,
    set_aside_structure,
    structure_problems,
)
from yizkr.pipeline.chunking import ChunkedClaimExtractor
from yizkr.pipeline.names import normalize_entity_dict
from yizkr.pipeline.transcribe import Transcription


def extract(tmp_path, document: str, manifest: str, claims: list[tuple]):
    """The batch the real extractor makes of `document` with the scripted
    engine, cited by the SHA-1 of the document's text."""
    artifact = ArtifactRef.from_bytes(document.encode())
    engine = ScriptedEngine([manifest, chunk_response(artifact, claims)])
    extractor = ChunkedClaimExtractor(
        engine, state_dir=tmp_path, budget=10_000
    )
    batch = extractor.extract(
        Transcription(artifact=artifact, text=document, engine="test"),
        artifact,
    )
    assert not extractor.used_fallback_manifest
    assert extractor.failed_chunks == []
    return batch


@pytest.mark.parametrize(
    ("aliases", "record"),
    [
        (FIRST_DOCUMENT_ALIASES, FIRST_RECORD),
        (SECOND_DOCUMENT_ALIASES, SECOND_RECORD),
    ],
)
def test_each_record_stands(aliases, record):
    """Each document's record adds no name part (ADR-0035 §5), so
    extraction keeps it."""
    raw = entity(aliases, 2, record)
    assert set_aside_structure(raw) == (raw, [])
    assert structure_problems(EntityRecord.model_validate(raw)) == []


def test_the_extractor_cleans_the_aliases_by_the_record(tmp_path):
    """What the live test checks before it writes: the first document's
    person keeps the forms that add a part, and the second's adds only
    'Ida Testerhof' to them."""
    first = extract(
        tmp_path / "first",
        FIRST_DOCUMENT,
        entity_manifest(FIRST_DOCUMENT_ALIASES, 2, FIRST_RECORD),
        FIRST_CLAIMS,
    )
    (person,) = first.entities
    assert person.label == LABEL
    assert person.structure is not None
    assert set(person.aliases) == EXPECTED_ALIASES - {"Ida Testerhof"}
    second = extract(
        tmp_path / "second",
        SECOND_DOCUMENT,
        entity_manifest(SECOND_DOCUMENT_ALIASES, 2, SECOND_RECORD),
        SECOND_CLAIMS,
    )
    (person,) = second.entities
    assert person.structure is not None
    assert set(person.aliases) == {"Peppi", "Ida Testerhof"}
    assert set(first.entities[0].aliases) | set(person.aliases) == (
        EXPECTED_ALIASES
    )


def test_a_person_without_a_record_keeps_her_aliases(tmp_path):
    """Nothing says which word of a recordless person's alias is a part,
    so the cleanup drops only blanks, the label and duplicates (ADR-0035
    §8)."""
    cleaned = normalize_entity_dict(entity(FIRST_DOCUMENT_ALIASES, 2, None))
    assert cleaned["aliases"] == [
        "Ida",
        "I. Livecheck",
        "Peppi",
        "Ida Probewald",
        "Livecheck-Probewald",
        "van der Probewald",
    ]
    batch = extract(
        tmp_path,
        FIRST_DOCUMENT,
        entity_manifest(FIRST_DOCUMENT_ALIASES, 2, None),
        FIRST_CLAIMS,
    )
    (person,) = batch.entities
    assert person.structure is None
    assert person.aliases == cleaned["aliases"]
