"""Live write paths for judgement notes, unions and refusals (#410).

A claim's judgement note lands in the run block of its statement, beside
`inferred from extraction run`, never in the `stated in` block or as a
qualifier, and a batch written again adds nothing, while a second claim
of the run with another note adds its own run block (ADR-0035 §4). An
entity's note lands in the run block of its item's `instance of`. A
union and its events, from an extraction's batch, land as items with
their kinds, roles, bounds, dates, places and court, each statement with
both references (ADR-0036, ADR-0003). A `spouse` claim, which validation
refuses, is refused by the writer too, since `yizkr write` takes a batch
without validating it. The interview's relative claim, Klara as
Nicholas's aunt, lands with its kinship type, kinship class and degree
as qualifiers (ADR-0035 §3.3). The tests write fictional fixture data
only (the universe's 1957 divorce judgment and the interview, under test
labels). Every title and
label carries the session's marker (conftest.fixture_mark), so each
session meets the fixture as a first write and a re-run within it
reuses its entities.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tests.fixtures.identity import write_mapped
from tests.integration.conftest import fixture_mark, register_fictional
from yizkr.claims import ClaimBatch, validate_against_manifest
from yizkr.pipeline import writer
from yizkr.pipeline.review import claim_id
from yizkr.pipeline.writer import BatchWriter

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#410: the 1957 divorce judgment's marriage of Leopold and Agnes, its "
    "civil wedding and its divorce, with judgement notes on a date and on "
    "the court's kind, and a spouse claim; and the interview's relative "
    "claim, Klara as Nicholas's aunt (kinship_type aunt, kinship_class "
    "kin_aunt_or_uncle, degree 0)"
)
MARK = fixture_mark(FIXTURE, writer)
STARTED = datetime.now(UTC).isoformat()

EXCERPT = (
    "Die am 8. Februar 1947 vor dem Standesbeamten in Flörburg "
    "geschlossene Ehe der Parteien wird geschieden"
)
DATE_NOTE = {
    "category": "date_reading",
    "text": "'8. Februar 1947' read as the day the marriage was concluded",
}
OTHER_NOTE = {
    "category": "date_reading",
    "text": "the wedding's date read as the judgment gives it, not as the "
    "date of its registration",
}
KIND_NOTE = {
    "category": "kind",
    "text": "the Landgericht Rekau typed an organisation: the judgment "
    "names it as the court that pronounced the divorce",
}


def local(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": f"{label} {MARK}",
        "instance_of": kind,
    }


def item(ref: dict) -> dict:
    return {"type": "item", "value": ref}


def role(key: str) -> list[dict]:
    return [
        {
            "property": "object_has_role",
            "value": item({"kind": "vocab", "key": key}),
        }
    ]


def batch_of(sha1: str, *claims: dict, entities=()) -> ClaimBatch:
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": STARTED,
            },
            "claims": [
                dict(
                    claim,
                    source={
                        "artifact": {"kind": "artifact", "sha1": sha1},
                        "page_number": claim.pop("page", "1"),
                        "transcription_excerpt": {
                            "text": EXCERPT,
                            "language": "de",
                        },
                    },
                )
                for claim in claims
            ],
            "entities": list(entities),
        }
    )


def statements(client, qid: str, key: str) -> list[dict]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {"action": "wbgetclaims", "entity": qid, "property": properties[key]}
    )
    return [claim for claims in data["claims"].values() for claim in claims]


def blocks(client, statement: dict) -> list[dict[str, list]]:
    """Each reference block of `statement`, by property key, each snak's
    value as an item id or a text."""
    properties, _ = client.vocabulary()
    keys = {pid: key for key, pid in properties.items()}
    shown = []
    for reference in statement.get("references", []):
        block: dict[str, list] = {}
        for pid, snaks in reference["snaks"].items():
            for snak in snaks:
                value = snak["datavalue"]["value"]
                if isinstance(value, dict):
                    value = value.get("id") or value.get("text")
                block.setdefault(keys[pid], []).append(value)
        shown.append(block)
    return shown


def run_blocks(client, statement: dict) -> list[tuple]:
    """Each run block of `statement` as (run item, category item, note),
    the last two None where it carries no note, sorted."""
    return sorted(
        (
            (
                block["inferred_from_extraction_run"][0],
                (block.get("judgement") or [None])[0],
                (block.get("judgement_note") or [None])[0],
            )
            for block in blocks(client, statement)
            if "inferred_from_extraction_run" in block
        ),
        key=lambda shown: tuple(part or "" for part in shown),
    )


def wedding(test: str) -> dict:
    """The civil wedding, under a label of the test `test`'s own: each
    test's entities are its own, never another test's found again."""
    return local(
        "e4", f"Yizkr Notes Test {test} civil wedding", "civil_wedding"
    )


def wedding_date(test: str, note: dict | None = None, page: str = "1") -> dict:
    claim = {
        "subject": wedding(test),
        "property": "point_in_time",
        "value": {"type": "time", "year": 1947, "month": 2, "day": 8},
        "page": page,
    }
    if note:
        claim["note"] = note
    return claim


def test_a_claims_note_lands_in_its_run_block_and_a_rerun_adds_nothing(
    client,
):
    judgment = register_fictional(
        client, f"Yizkr Notes Test Divorce Judgment {MARK}", "certificate"
    )
    _, items = client.vocabulary()
    batch = batch_of(judgment.ref.sha1, wedding_date("one note", DATE_NOTE))
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert (result.written, result.notes_written) == (1, 1)
    (statement,) = statements(
        client, result.created_items["e4"], "point_in_time"
    )
    source_block, run_block = blocks(client, statement)
    assert source_block["stated_in"] == [judgment.qid]
    assert "judgement" not in source_block
    assert "judgement_note" not in source_block
    assert not statement.get("qualifiers")
    assert run_block == {
        "inferred_from_extraction_run": [result.run_qid],
        "judgement": [items["judgement_date_reading"]],
        "judgement_note": [DATE_NOTE["text"]],
    }

    # the same batch again: the run block, compared whole as the instance
    # gives it back, is found, and nothing is added
    again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.referenced, again.skipped) == (0, 0, 1)
    assert again.notes_written == 0
    (statement,) = statements(
        client, result.created_items["e4"], "point_in_time"
    )
    assert len(statement["references"]) == 2


def test_a_second_claim_of_the_run_with_another_note_adds_its_block(client):
    judgment = register_fictional(
        client,
        f"Yizkr Notes Test Divorce Judgment, two notes {MARK}",
        "certificate",
    )
    batch = batch_of(
        judgment.ref.sha1,
        wedding_date("two notes", DATE_NOTE),
        wedding_date("two notes", OTHER_NOTE),
    )
    result = write_mapped(BatchWriter(client), batch)
    assert (result.written, result.referenced) == (1, 1)
    assert result.notes_written == 2
    (statement,) = statements(
        client, result.created_items["e4"], "point_in_time"
    )
    shown = blocks(client, statement)
    assert [block for block in shown if "stated_in" in block] == [
        {
            "stated_in": [judgment.qid],
            "page_number": ["1"],
            "transcription_excerpt": [EXCERPT],
        }
    ]
    assert [note for _, _, note in run_blocks(client, statement)] == sorted(
        [DATE_NOTE["text"], OTHER_NOTE["text"]]
    )
    again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.referenced, again.skipped) == (0, 0, 2)
    (statement,) = statements(
        client, result.created_items["e4"], "point_in_time"
    )
    assert len(statement["references"]) == 3


def test_an_entitys_note_lands_on_its_instance_of(client):
    judgment = register_fictional(
        client,
        f"Yizkr Notes Test Divorce Judgment, the court {MARK}",
        "certificate",
    )
    _, items = client.vocabulary()
    divorce = local("e5", "Yizkr Notes Test court's divorce", "divorce")
    court = local(
        "e6", "Yizkr Notes Test court's Landgericht Rekau", "organization"
    )
    batch = batch_of(
        judgment.ref.sha1,
        {
            "subject": divorce,
            "property": "carried_out_by",
            "value": item(court),
        },
        entities=[
            {"id": "e5", "label": divorce["label"], "kind": "divorce"},
            {
                "id": "e6",
                "label": court["label"],
                "kind": "organization",
                "note": KIND_NOTE,
            },
        ],
    )
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert result.notes_written == 1
    (kind,) = statements(client, result.created_items["e6"], "instance_of")
    assert run_blocks(client, kind) == [
        (result.run_qid, items["judgement_kind"], KIND_NOTE["text"])
    ]
    # the claim that mentions the court carries no note of its own
    (claim,) = statements(client, result.created_items["e5"], "carried_out_by")
    assert run_blocks(client, claim) == [(result.run_qid, None, None)]
    again = write_mapped(BatchWriter(client), batch)
    assert (again.notes_written, again.skipped) == (0, 1)
    (kind,) = statements(client, result.created_items["e6"], "instance_of")
    assert len(kind["references"]) == 2


def test_a_union_and_its_events_land_with_their_references(client):
    """The judgment's marriage: its couple take part with the role spouse,
    it is started by a civil wedding dated 8 February 1947 at Flörburg and
    ended by a divorce dated 2 January 1957, carried out by the court."""
    judgment = register_fictional(
        client,
        f"Yizkr Notes Test Divorce Judgment, the union {MARK}",
        "certificate",
    )
    _, items = client.vocabulary()
    leopold = local("e1", "Yizkr Notes Test Leopold Löbig", "human")
    agnes = local("e2", "Yizkr Notes Test Agnes Löbig", "human")
    union = local("e3", "Yizkr Notes Test marriage of Leopold", "marriage")
    begun = wedding("union")
    divorce = local("e5", "Yizkr Notes Test union divorce", "divorce")
    court = local(
        "e6", "Yizkr Notes Test union Landgericht Rekau", "organization"
    )
    town = local("e7", "Yizkr Notes Test Flörburg", "place")
    claims = [
        {
            "subject": union,
            "property": "participant",
            "value": item(leopold),
            "qualifiers": role("spouse"),
        },
        {
            "subject": union,
            "property": "participant",
            "value": item(agnes),
            "qualifiers": role("spouse"),
        },
        {"subject": union, "property": "started_by", "value": item(begun)},
        {"subject": union, "property": "ended_by", "value": item(divorce)},
        wedding_date("union"),
        {"subject": begun, "property": "location", "value": item(town)},
        {
            "subject": divorce,
            "property": "point_in_time",
            "value": {"type": "time", "year": 1957, "month": 1, "day": 2},
        },
        {
            "subject": divorce,
            "property": "carried_out_by",
            "value": item(court),
        },
    ]
    batch = batch_of(judgment.ref.sha1, *claims)
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert (result.written, result.refused) == (8, [])
    made = result.created_items

    def kinds(qid):
        return {
            s["mainsnak"]["datavalue"]["value"]["id"]
            for s in statements(client, qid, "instance_of")
        }

    assert kinds(made["e3"]) == {items["marriage"]}
    assert kinds(made["e4"]) == {items["civil_wedding"]}
    assert kinds(made["e5"]) == {items["divorce"]}
    properties, _ = client.vocabulary()
    couple = statements(client, made["e3"], "participant")
    assert {s["mainsnak"]["datavalue"]["value"]["id"] for s in couple} == {
        made["e1"],
        made["e2"],
    }
    for statement in couple:
        (snak,) = statement["qualifiers"][properties["object_has_role"]]
        assert snak["datavalue"]["value"]["id"] == items["spouse"]
    (started,) = statements(client, made["e3"], "started_by")
    assert started["mainsnak"]["datavalue"]["value"]["id"] == made["e4"]
    (ended,) = statements(client, made["e3"], "ended_by")
    assert ended["mainsnak"]["datavalue"]["value"]["id"] == made["e5"]
    (place,) = statements(client, made["e4"], "location")
    assert place["mainsnak"]["datavalue"]["value"]["id"] == made["e7"]
    (court_of,) = statements(client, made["e5"], "carried_out_by")
    assert court_of["mainsnak"]["datavalue"]["value"]["id"] == made["e6"]
    # the couple take part in the union alone, never in its wedding
    assert statements(client, made["e4"], "participant") == []
    for key in ("e3", "e4", "e5"):
        for statement in client.api_get(
            {"action": "wbgetclaims", "entity": made[key]}
        )["claims"].values():
            for one in statement:
                shown = blocks(client, one)
                assert [b["stated_in"] for b in shown if "stated_in" in b] == [
                    [judgment.qid]
                ]
                assert run_blocks(client, one) == [
                    (result.run_qid, None, None)
                ]
    again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.skipped, again.referenced) == (0, 8, 0)


def test_a_spouse_claim_is_refused(client):
    """`yizkr write` takes a batch without validating it, so the writer
    refuses the retired property itself, before any of its refs resolves,
    and writes the rest."""
    judgment = register_fictional(
        client,
        f"Yizkr Notes Test Divorce Judgment, a spouse claim {MARK}",
        "certificate",
    )
    leopold = local("e1", "Yizkr Notes Test Leopold, spouse claim", "human")
    agnes = local("e2", "Yizkr Notes Test Agnes, spouse claim", "human")
    batch = batch_of(
        judgment.ref.sha1,
        {"subject": leopold, "property": "spouse", "value": item(agnes)},
        wedding_date("spouse claim"),
    )
    assert validate_against_manifest(batch) != []
    result = write_mapped(BatchWriter(client), batch)
    assert result.refused == [claim_id(batch.claims[0])]
    assert result.written == 1
    assert "e1" not in result.created_items
    assert "e2" not in result.created_items
    assert any("is retired" in warning for warning in result.warnings)
    # nothing anywhere on the instance cites the judgment by a spouse
    # statement: the wedding's date is all it gave
    (statement,) = statements(
        client, result.created_items["e4"], "point_in_time"
    )
    assert blocks(client, statement)[0]["stated_in"] == [judgment.qid]


def test_kinship_class_qualifiers_land_and_a_rerun_adds_nothing(client):
    """The interview's "Aunt Pünktchen": Klara, Nicholas's relative, with
    `kinship_type` aunt, class aunt or uncle, and degree 0 (ADR-0035 §3.3).
    The quantity qualifier 0 lands as "+0" and is found again on a
    re-run."""
    interview = register_fictional(
        client, f"Yizkr Notes Test Interview with Nicholas {MARK}", "artifact"
    )
    properties, items = client.vocabulary()
    klara = local("i1", "Yizkr Notes Test Klara Löbig", "human")
    nicholas = local("i2", "Yizkr Notes Test Nicholas Lobig", "human")
    batch = batch_of(
        interview.ref.sha1,
        {
            "subject": klara,
            "property": "relative",
            "value": item(nicholas),
            "qualifiers": [
                {
                    "property": "kinship_type",
                    "value": {"type": "string", "value": "aunt"},
                },
                {
                    "property": "kinship_class",
                    "value": item(
                        {"kind": "vocab", "key": "kin_aunt_or_uncle"}
                    ),
                },
                {
                    "property": "degree",
                    "value": {"type": "quantity", "amount": 0},
                },
            ],
        },
    )
    assert validate_against_manifest(batch) == []
    result = write_mapped(BatchWriter(client), batch)
    assert result.written == 1
    (statement,) = statements(client, result.created_items["i1"], "relative")
    assert (
        statement["mainsnak"]["datavalue"]["value"]["id"]
        == result.created_items["i2"]
    )
    qualifiers = statement["qualifiers"]
    (kin,) = qualifiers[properties["kinship_class"]]
    assert kin["datavalue"]["value"]["id"] == items["kin_aunt_or_uncle"]
    (degree,) = qualifiers[properties["degree"]]
    assert degree["datavalue"]["value"]["amount"] == "+0"
    (term,) = qualifiers[properties["kinship_type"]]
    assert term["datavalue"]["value"] == "aunt"
    again = write_mapped(BatchWriter(client), batch)
    assert (again.written, again.skipped) == (0, 1)
