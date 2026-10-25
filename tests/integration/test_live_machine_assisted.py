"""Live write path for the machine-assisted marker (#666, ADR-0008).

A compilation that says its translations were made with translation
software marks the claims read from its translation with
`sourcing_circumstances` -> `machine_assisted`, a manifest item the
bootstrap puts on the instance. The writer writes it as it writes circa,
so the compilation's value is a statement of its own beside the
identical value from the original it translates, each with its own
source, and the marked statement also cites, in a reference block of its
own, the compilation's statement about its translations, on its page 2.
A re-run adds nothing. The fixture is the fictional universe's: *Our
Family from Germany* gives Gustav's nickname "Gustl" from its
translation of the 1939 letter, and the letter's own image gives it from
"Gustl hat endlich sein Visum". Every title and label carries the
session's marker (conftest.fixture_mark), so each session meets the
fixture as a first write and a re-run within it finds its own items.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import joined, mapped
from tests.integration.conftest import fixture_mark, register_fictional
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline import preparation, registrar, writer
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#666: Gustav's nickname Gustl as the 2011 booklet's machine-assisted "
    "translation gives it, and as the 1939 letter's image gives it"
)
MARK = fixture_mark(FIXTURE, registrar, writer, preparation)
BOOKLET = f"Yizkr Machine-Assisted Test Our Family from Germany {MARK}"
LETTER = f"Yizkr Machine-Assisted Test Letter Excerpt 1939 {MARK}"
PERSON = f"Yizkr Machine-Assisted Test Gustav Löbig {MARK}"
MARKER = {
    "property": "sourcing_circumstances",
    "value": {
        "type": "item",
        "value": {"kind": "vocab", "key": "machine_assisted"},
    },
}


# the booklet's statement about its translations, on its page 2
STATEMENT = (
    "All translations in this booklet were done with translation software"
)


def _batch(sha1: str, excerpt: str, language: str, marked: bool) -> ClaimBatch:
    """Gustav's nickname "Gustl", one datavalue for both witnesses, read
    from `excerpt`, in its own language; the booklet's marked, with the
    statement its marker stands on."""
    stated = {
        "artifact": {"kind": "artifact", "sha1": sha1},
        "page_number": "2",
        "transcription_excerpt": {"text": STATEMENT, "language": "en"},
    }
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "e1",
                        "label": PERSON,
                    },
                    "property": "nickname",
                    "value": {
                        "type": "monolingual",
                        "value": {"text": "Gustl", "language": "de"},
                    },
                    "qualifiers": [MARKER] if marked else [],
                    "source": {
                        "artifact": {"kind": "artifact", "sha1": sha1},
                        "page_number": "6" if marked else None,
                        "transcription_excerpt": {
                            "text": excerpt,
                            "language": language,
                        },
                    },
                }
                | ({"circumstance_source": stated} if marked else {})
            ],
            "entities": [{"id": "e1", "label": PERSON, "kind": "human"}],
        }
    )


def _statements(client, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": properties[key]}
    )
    return [claim for claims in data["claims"].values() for claim in claims]


def _cited(client, statement: dict) -> set[str]:
    properties, _ = client.vocabulary()
    return {
        snak["datavalue"]["value"]["id"]
        for reference in statement.get("references", [])
        for snak in reference["snaks"].get(properties["stated_in"], [])
    }


def _blocks(client, statement: dict) -> set[tuple]:
    """Each source block of a statement, as (page number, excerpt)."""
    properties, _ = client.vocabulary()

    def first(reference, key):
        snaks = reference["snaks"].get(properties[key], [])
        if not snaks:
            return None
        value = snaks[0]["datavalue"]["value"]
        return value["text"] if isinstance(value, dict) else value

    return {
        (
            first(reference, "page_number"),
            first(reference, "transcription_excerpt"),
        )
        for reference in statement.get("references", [])
        if reference["snaks"].get(properties["stated_in"])
    }


def _circumstances(client, statement: dict) -> set[str]:
    properties, _ = client.vocabulary()
    return {
        snak["datavalue"]["value"]["id"]
        for snak in statement.get("qualifiers", {}).get(
            properties["sourcing_circumstances"], []
        )
    }


def test_a_marked_claim_is_a_statement_of_its_own(client):
    _, items = client.vocabulary()
    # the bootstrap put the manifest's item on the instance
    assert "machine_assisted" in items
    booklet = register_fictional(client, BOOKLET, "artifact")
    letter = register_fictional(client, LETTER, "photograph")
    translated = _batch(
        booklet.ref.sha1, "Gustl finally has his visa", "en", True
    )
    original = _batch(
        letter.ref.sha1, "Gustl hat endlich sein Visum", "de", False
    )
    # one datavalue: only the marker keeps the two statements apart
    assert translated.claims[0].value == original.claims[0].value
    for batch in (translated, original):
        assert validate_against_manifest(batch) == []
    identity = joined(mapped(translated, original), PERSON)

    first = BatchWriter(client).write_batch(
        translated, identity=identity, one_map=False
    )
    second = BatchWriter(client).write_batch(
        original, identity=identity, one_map=False
    )
    assert (first.written, second.written) == (1, 1)
    gustav = first.created_items["e1"]
    assert (second.created_items.get("e1") or gustav) == gustav

    statements = _statements(client, gustav, "nickname")
    by_source = {
        frozenset(_cited(client, statement)): statement
        for statement in statements
    }
    assert set(by_source) == {
        frozenset({booklet.qid}),
        frozenset({letter.qid}),
    }
    marked = by_source[frozenset({booklet.qid})]
    assert _circumstances(client, marked) == {items["machine_assisted"]}
    assert marked["rank"] == "normal"
    # its own citation, and the statement its marker stands on, each a
    # block of its own in the booklet
    assert _blocks(client, marked) == {
        ("6", "Gustl finally has his visa"),
        ("2", STATEMENT),
    }
    # the original's statement carries no marker: never a rank, never the
    # compilation's circumstance spread to another witness
    unmarked = by_source[frozenset({letter.qid})]
    assert _circumstances(client, unmarked) == set()
    assert unmarked["rank"] == "normal"
    assert _blocks(client, unmarked) == {
        (None, "Gustl hat endlich sein Visum")
    }

    # a re-run finds both statements, sourced, and adds nothing
    for batch in (translated, original):
        again = BatchWriter(client).write_batch(
            batch, identity=identity, one_map=False
        )
        assert (again.written, again.skipped) == (0, 1)
    assert len(_statements(client, gustav, "nickname")) == 2
