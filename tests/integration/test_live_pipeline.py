"""Live pipeline flow: register -> write, idempotency, the overwrite
regression (two values on one property must COEXIST — the second write
must never replace the first; ADR-0002), the provenance of a minted
item's type statements, which the reappraisal queue and the unreferenced
report must count correctly (#88), and a later source's kind for an item
that already exists, which the queue must reflect (#233)."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from tests.fixtures import example_universe as u
from yizkr.claims import ClaimBatch, QidRef
from yizkr.pipeline.reconcile import apply_equivalences
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .conftest import run_sparql, wait_for_query_service

pytestmark = pytest.mark.live

ARTIFACT_TITLE = "Integration-test certificate"
PERSON_LABEL = "Integration Test Person"
# the example universe's family firm, under this file's label prefix
FIRM_LABEL = "Integration Test Hutmacherei zum Blauen Hut"


def png_bytes(fill: bytes = b"\x32\x32\xc8") -> bytes:
    """A minimal valid 4x4 PNG; varying `fill` (an RGB triple) varies the
    content hash, so callers can make identical or distinct images."""

    def chunk(tag, data):
        payload = tag + data
        return (
            struct.pack(">I", len(data))
            + payload
            + struct.pack(">I", zlib.crc32(payload))
        )

    raw = b"".join(b"\x00" + fill * 4 for _ in range(4))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def tiny_png(tmp_path):
    path = tmp_path / "integration-test-certificate.png"
    path.write_bytes(png_bytes())
    return path


def birth_batch(artifact_qid: str, year: int) -> ClaimBatch:
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
                        "local_id": "person-1",
                        "label": PERSON_LABEL,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": year},
                    "source": {
                        "artifact": {"kind": "qid", "qid": artifact_qid},
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": f"born {year}",
                            "language": "en",
                        },
                    },
                }
            ],
        }
    )


@pytest.fixture(scope="module")
def artifact(client, tmp_path_factory):
    media = tiny_png(tmp_path_factory.mktemp("media"))
    return register_artifact(
        client,
        media_path=media,
        artifact_type="certificate",
        title=ARTIFACT_TITLE,
        description="written by tests/integration; safe to delete",
        pages=1,
    )


def test_register_creates_artifact_fragment_and_media(client, artifact):
    assert artifact.artifact_qid.startswith("Q")
    assert artifact.fragment_qids[1].startswith("Q")
    assert artifact.file_page.startswith("File:")


def test_register_is_idempotent(client, artifact):
    again = register_artifact(
        client,
        media_path=None,
        artifact_type="certificate",
        title=ARTIFACT_TITLE,
        pages=1,
    )
    assert again.created is False
    assert again.artifact_qid == artifact.artifact_qid
    assert again.fragment_qids == artifact.fragment_qids


def _sha1_values(client, qid):
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["media_sha1"],
        }
    )
    return [
        claim["mainsnak"]["datavalue"]["value"]
        for claims in data["claims"].values()
        for claim in claims
    ]


def test_identical_bytes_new_name_reuse_one_artifact(client, tmp_path):
    """ADR-0010: the same bytes under a different filename resolve to the
    one artifact item (keyed on content hash, not the title), and the new
    filename is kept as an alias — no duplicate item sharing a blob."""
    content = png_bytes(fill=b"\x11\x77\x22")
    first_path = tmp_path / "hash-key-original.png"
    first_path.write_bytes(content)
    first = register_artifact(
        client,
        media_path=first_path,
        artifact_type="photograph",
        title="Hash Key Original",
    )

    second_path = tmp_path / "hash-key-copy.png"  # identical bytes, new name
    second_path.write_bytes(content)
    second = register_artifact(
        client,
        media_path=second_path,
        artifact_type="photograph",
        title="Hash Key Copy",
    )

    assert second.artifact_qid == first.artifact_qid  # one artifact, not two
    assert second.created is False
    # the content hash is stored once
    assert len(_sha1_values(client, first.artifact_qid)) == 1
    # the alternate filename-title became an alias
    item = client.wbi.item.get(first.artifact_qid)
    aliases = {str(alias) for alias in (item.aliases.get("en") or [])}
    assert "Hash Key Copy" in aliases


def test_same_title_different_content_stays_distinct(client, tmp_path):
    """A re-scan/re-encode (different bytes) under a title that already
    names other content must not collapse onto it: labels stay unique via
    a content-tag suffix, and the two remain separate artifacts for the
    near-duplicate pass to relate later."""
    path_a = tmp_path / "collide-a.png"
    path_a.write_bytes(png_bytes(fill=b"\x01\x02\x03"))
    a = register_artifact(
        client,
        media_path=path_a,
        artifact_type="photograph",
        title="Collision Title",
    )

    path_b = tmp_path / "collide-b.png"  # DIFFERENT bytes, same intended title
    path_b.write_bytes(png_bytes(fill=b"\xaa\xbb\xcc"))
    b = register_artifact(
        client,
        media_path=path_b,
        artifact_type="photograph",
        title="Collision Title",
    )

    assert b.artifact_qid != a.artifact_qid
    label_a = str(client.wbi.item.get(a.artifact_qid).labels.get("en"))
    label_b = str(client.wbi.item.get(b.artifact_qid).labels.get("en"))
    assert label_a == "Collision Title"
    assert label_b.startswith("Collision Title [")  # disambiguated


def birth_years_on(client, qid: str) -> set[str]:
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": properties["date_of_birth"],
        }
    )
    return {
        claim["mainsnak"]["datavalue"]["value"]["time"]
        for claims in data["claims"].values()
        for claim in claims
    }


def test_conflicting_values_coexist(client, artifact):
    """Writing a second value must never delete the first (ADR-0002).

    Regression for the WikibaseIntegrator REPLACE_ALL default that once
    silently overwrote a source-attributed statement.
    """
    writer = BatchWriter(client)
    first = writer.write_batch(birth_batch(artifact.artifact_qid, 1901))
    person_qid = first.created_items["person-1"]

    writer = BatchWriter(client)  # fresh entity cache
    writer.write_batch(birth_batch(artifact.artifact_qid, 1902))

    years = birth_years_on(client, person_qid)
    assert "+1901-00-00T00:00:00Z" in years
    assert "+1902-00-00T00:00:00Z" in years


def test_write_is_idempotent_and_dual_referenced(client, artifact):
    writer = BatchWriter(client)
    result = writer.write_batch(birth_batch(artifact.artifact_qid, 1901))
    assert result.written == 0
    assert result.skipped == 1

    properties, _ = client.vocabulary()
    person_qid = result.created_items["person-1"]
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    for claims in data["claims"].values():
        for claim in claims:
            assert claim["rank"] == "normal"  # ranks default to normal
            reference_properties = {
                pid for ref in claim["references"] for pid in ref["snaks"]
            }
            assert (
                properties["stated_in"] in reference_properties
            )  # source ref
            assert properties["inferred_from_extraction_run"] in (
                reference_properties
            )  # process ref


def stated_in_values(
    client, person_qid: str, year_time: str
) -> tuple[int, set[str]]:
    """(statement count for the year, set of stated-in artifact QIDs)."""
    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    matching = [
        claim
        for claims in data["claims"].values()
        for claim in claims
        if claim["mainsnak"]["datavalue"]["value"]["time"] == year_time
    ]
    sources = {
        snak["datavalue"]["value"]["id"]
        for claim in matching
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(properties["stated_in"], [])
    }
    return len(matching), sources


def test_agreeing_second_source_appends_reference(client, artifact):
    """A second source asserting the SAME value must not vanish: the
    existing statement gains that source's reference block (R2 — who
    asserts a fact is knowledge), and stays one statement."""
    second = register_artifact(
        client,
        media_path=None,
        artifact_type="certificate",
        title=f"{ARTIFACT_TITLE} (second source)",
        description="written by tests/integration; safe to delete",
    )
    writer = BatchWriter(client)
    result = writer.write_batch(birth_batch(second.artifact_qid, 1901))
    assert result.written == 0
    # first suite run appends the reference; later runs find it present
    assert (result.referenced, result.skipped) in ((1, 0), (0, 1))
    person_qid = result.created_items["person-1"]

    count, sources = stated_in_values(
        client, person_qid, "+1901-00-00T00:00:00Z"
    )
    assert count == 1  # still one statement, not a duplicate
    assert {artifact.artifact_qid, second.artifact_qid} <= sources

    # re-running the same batch is a no-op (reference already present)
    rerun = BatchWriter(client).write_batch(
        birth_batch(second.artifact_qid, 1901)
    )
    assert rerun.written == 0
    assert rerun.referenced == 0
    assert rerun.skipped == 1


def test_curated_claim_carries_no_run_reference(client, artifact):
    """A curator-attributed claim (a value correction) is a human
    write: artifact reference only, no extraction-run reference
    (ADR-0003 — process provenance is for machine writes)."""
    batch = birth_batch(artifact.artifact_qid, 1990)
    death = batch.claims[0].model_copy(
        update={"property": "date_of_death", "curated": True}
    )
    curated_batch = batch.model_copy(update={"claims": [death]})
    writer = BatchWriter(client)
    result = writer.write_batch(curated_batch)
    person_qid = result.created_items["person-1"]

    properties, _ = client.vocabulary()
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_death"],
        }
    )
    (claim,) = [
        claim
        for claims in data["claims"].values()
        for claim in claims
        if claim["mainsnak"]["datavalue"]["value"]["time"].startswith("+1990")
    ]
    reference_properties = {
        pid for ref in claim["references"] for pid in ref["snaks"]
    }
    assert properties["stated_in"] in reference_properties
    assert (
        properties["inferred_from_extraction_run"] not in reference_properties
    )


def test_detected_language_tags_excerpt_reference(client, artifact):
    """ADR-0015: a document the pipeline detects as non-English writes its
    source excerpt with that language tag. The detected code (here `de`,
    chosen by the real detector, not hand-set) must round-trip through the
    reference snak — proving Wikibase accepts a detected tag end to end."""
    from yizkr.pipeline.language import AUTO, resolve_language

    # Eugen Löbig's life in the example universe, spelled out and
    # transliterated as a typed transcript might give it
    german = (
        "Geboren im Jahr achtzehnhundertsechsunddreissig in Floerburg, "
        "gestorben am sechzehnten Maerz neunzehnhundertneun in Floerburg."
    )
    resolution = resolve_language(german, AUTO)
    assert resolution.code == "de"  # the pipeline picks the language itself

    batch = ClaimBatch.model_validate(
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
                        "local_id": "person-1",
                        "label": PERSON_LABEL,
                        "instance_of": "human",
                    },
                    "property": "date_of_birth",
                    "value": {"type": "time", "year": 1836},
                    "source": {
                        "artifact": {
                            "kind": "qid",
                            "qid": artifact.artifact_qid,
                        },
                        "page_number": "1",
                        "transcription_excerpt": {
                            "text": german[:60],
                            "language": resolution.code,
                        },
                    },
                }
            ],
        }
    )
    result = BatchWriter(client).write_batch(batch)
    person_qid = result.created_items["person-1"]

    properties, _ = client.vocabulary()
    excerpt_pid = properties["transcription_excerpt"]
    data = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["date_of_birth"],
        }
    )
    # find the excerpt this test wrote (by its German wording) and confirm
    # its reference snak carries the detected `de` tag
    excerpts = [
        snak["datavalue"]["value"]
        for claims in data["claims"].values()
        for claim in claims
        for ref in claim.get("references", [])
        for snak in ref["snaks"].get(excerpt_pid, [])
    ]
    german_excerpts = [e for e in excerpts if e["text"] == german[:60]]
    assert german_excerpts, "the German excerpt was not written"
    assert all(e["language"] == "de" for e in german_excerpts)


def vocabulary_batch(artifact_qid: str) -> ClaimBatch:
    """One claim per item-10 vocabulary addition (quantity, monolingual
    cause, occupation item, burial place)."""
    subject = {
        "kind": "local",
        "local_id": "person-1",
        "label": PERSON_LABEL,
        "instance_of": "human",
    }
    source = {
        "artifact": {"kind": "qid", "qid": artifact_qid},
        "page_number": "1",
        "transcription_excerpt": {
            "text": "occupation, burial, cause of death, children",
            "language": "en",
        },
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
                    "subject": subject,
                    "property": "occupation",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "occ-1",
                            "label": "Integration test occupation",
                            "instance_of": "occupation_class",
                        },
                    },
                    "source": source,
                },
                {
                    "subject": subject,
                    "property": "burial_place",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "pl-1",
                            "label": "Integration Test Cemetery",
                            "instance_of": "place",
                        },
                    },
                    "source": source,
                },
                {
                    "subject": subject,
                    "property": "cause_of_death",
                    "value": {
                        "type": "monolingual",
                        "value": {
                            "text": "integration test cause",
                            "language": "en",
                        },
                    },
                    "source": source,
                },
                {
                    "subject": subject,
                    "property": "number_of_children",
                    "value": {"type": "quantity", "amount": 2},
                    "source": source,
                },
            ],
        }
    )


def test_item10_vocabulary_writes_and_is_idempotent(client, artifact):
    """Item-10 vocabulary (occupation, burial place, cause of death,
    number of children) writes end to end: the quantity datavalue and
    verbatim monolingual cause round-trip, the occupation item is an
    instance of the occupation class, and a re-run writes nothing."""
    writer = BatchWriter(client)
    first = writer.write_batch(vocabulary_batch(artifact.artifact_qid))
    person_qid = first.created_items["person-1"]
    occupation_qid = first.created_items["occ-1"]

    rerun = BatchWriter(client).write_batch(
        vocabulary_batch(artifact.artifact_qid)
    )
    assert rerun.written == 0
    assert rerun.skipped == 4

    properties, items = client.vocabulary()

    def claim_values(entity, prop_key):
        data = client.api_get(
            {
                "action": "wbgetclaims",
                "entity": entity,
                "property": properties[prop_key],
            }
        )
        return [
            claim["mainsnak"]["datavalue"]["value"]
            for claims in data["claims"].values()
            for claim in claims
        ]

    (quantity,) = claim_values(person_qid, "number_of_children")
    assert quantity["amount"] == "+2"
    (cause,) = claim_values(person_qid, "cause_of_death")
    assert cause == {"text": "integration test cause", "language": "en"}
    burial_targets = {
        value["id"] for value in claim_values(person_qid, "burial_place")
    }
    assert first.created_items["pl-1"] in burial_targets
    occupation_classes = {
        value["id"] for value in claim_values(occupation_qid, "instance_of")
    }
    assert items["occupation_class"] in occupation_classes


def names_batch(artifact_qid: str) -> ClaimBatch:
    """A person carrying typed name claims (ADR-0011)."""
    subject = {
        "kind": "local",
        "local_id": "person-1",
        "label": PERSON_LABEL,
        "instance_of": "human",
    }
    source = {
        "artifact": {"kind": "qid", "qid": artifact_qid},
        "page_number": "1",
        "transcription_excerpt": {
            "text": "née, later, known as",
            "language": "en",
        },
    }

    def name(prop, text):
        return {
            "subject": subject,
            "property": prop,
            "value": {
                "type": "monolingual",
                "value": {"text": text, "language": "en"},
            },
            "source": source,
        }

    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                name("birth_name", "Integration Testerson"),
                name("married_name", "Integration Person"),
                name("nickname", "Iggy"),
            ],
        }
    )


def test_typed_names_write_and_round_trip(client, artifact):
    """ADR-0011: typed name claims write end to end as referenced
    monolingual statements, keep the source's wording, and re-run clean."""
    writer = BatchWriter(client)
    first = writer.write_batch(names_batch(artifact.artifact_qid))
    person_qid = first.created_items["person-1"]

    rerun = BatchWriter(client).write_batch(names_batch(artifact.artifact_qid))
    assert rerun.written == 0
    assert rerun.skipped == 3

    properties, _ = client.vocabulary()

    def one(prop_key):
        data = client.api_get(
            {
                "action": "wbgetclaims",
                "entity": person_qid,
                "property": properties[prop_key],
            }
        )
        (claim,) = [c for cs in data["claims"].values() for c in cs]
        assert properties["stated_in"] in {
            pid for ref in claim["references"] for pid in ref["snaks"]
        }
        return claim["mainsnak"]["datavalue"]["value"]

    assert one("birth_name") == {
        "text": "Integration Testerson",
        "language": "en",
    }
    assert one("married_name") == {
        "text": "Integration Person",
        "language": "en",
    }
    assert one("nickname") == {"text": "Iggy", "language": "en"}


def harness_batch(artifact_qid: str) -> ClaimBatch:
    """An organization + employer relation, and an unclassified entity
    carrying a type hint (ADR-0012 / ADR-0013)."""
    source = {
        "artifact": {"kind": "qid", "qid": artifact_qid},
        "page_number": "1",
        "transcription_excerpt": {
            "text": "worked at the hat shop, the Blue Hat",
            "language": "en",
        },
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
                        "local_id": "person-1",
                        "label": PERSON_LABEL,
                        "instance_of": "human",
                    },  # fmt: skip
                    "property": "employer",
                    "value": {
                        "type": "item",
                        "value": {
                            "kind": "local",
                            "local_id": "org-1",
                            "label": FIRM_LABEL,
                            "instance_of": "organization",
                        },
                    },  # fmt: skip
                    "source": source,
                },
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "ship-1",
                        "label": "SS Integration",
                        "instance_of": "unclassified_entity",
                    },  # fmt: skip
                    "property": "point_in_time",
                    "value": {"type": "time", "year": 1912},
                    "source": source,
                },
            ],
            "entities": [
                {"id": "person-1", "label": PERSON_LABEL, "kind": "human"},
                {
                    "id": "org-1",
                    "label": FIRM_LABEL,
                    "kind": "organization",
                },  # fmt: skip
                {
                    "id": "ship-1",
                    "label": "SS Integration",
                    "kind": "unclassified_entity",
                    "type_hint": "ocean liner",
                },  # fmt: skip
            ],
        }
    )


def test_organization_employer_and_type_hint_write(client, artifact):
    """ADR-0012/0013: an organization is written and linked by employer;
    an unclassified entity is written under the holding class with its
    type hint — never mistyped as a person or place."""
    writer = BatchWriter(client)
    result = writer.write_batch(harness_batch(artifact.artifact_qid))
    person_qid = result.created_items["person-1"]
    org_qid = result.created_items["org-1"]
    ship_qid = result.created_items["ship-1"]
    properties, items = client.vocabulary()

    def claim_values(entity, prop_key):
        data = client.api_get(
            {
                "action": "wbgetclaims",
                "entity": entity,
                "property": properties[prop_key],
            }  # fmt: skip
        )
        return [
            c["mainsnak"]["datavalue"]["value"]
            for cs in data["claims"].values()
            for c in cs
        ]

    # employer links the person to the organization item
    assert org_qid in {v["id"] for v in claim_values(person_qid, "employer")}
    assert items["organization"] in {
        v["id"] for v in claim_values(org_qid, "instance_of")
    }
    # the unclassified entity is held under the holding class, hint recorded
    assert items["unclassified_entity"] in {
        v["id"] for v in claim_values(ship_qid, "instance_of")
    }
    assert "ocean liner" in claim_values(ship_qid, "type_hint")


def kinship_batch(artifact_qid: str) -> ClaimBatch:
    """ADR-0014: sex_or_gender (vocab), the `relative` + kinship_type
    fallback, and a deportation event. The named relative is the
    `relative` statement's subject and the ego its object (ADR-0018)."""
    source = {
        "artifact": {"kind": "qid", "qid": artifact_qid},
        "page_number": "1",
        "transcription_excerpt": {"text": "aunt; deported", "language": "en"},
    }
    person = {"kind": "local", "local_id": "person-1", "label": PERSON_LABEL,
              "instance_of": "human"}  # fmt: skip
    aunt = {"kind": "local", "local_id": "aunt-1", "label": "Integration Aunt",
            "instance_of": "human"}  # fmt: skip
    return ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "integration-test",
                "pipeline_version": "0.0.0",
                "started_at": datetime.now(UTC).isoformat(),
            },
            "claims": [
                {
                    "subject": person,
                    "property": "sex_or_gender",
                    "value": {
                        "type": "item",
                        "value": {"kind": "vocab", "key": "male"},
                    },
                    "source": source,
                },  # fmt: skip
                {
                    "subject": aunt,
                    "property": "relative",
                    "value": {"type": "item", "value": person},
                    "qualifiers": [
                        {
                            "property": "kinship_type",
                            "value": {
                                "type": "string",
                                "value": "maternal aunt",
                            },
                        }
                    ],
                    "source": source,
                },  # fmt: skip
                {
                    "subject": {
                        "kind": "local",
                        "local_id": "event-1",
                        "label": "Deportation of Integration Test Person",
                        "instance_of": "deportation",
                    },
                    "property": "participant",
                    "value": {"type": "item", "value": person},
                    "qualifiers": [
                        {
                            "property": "point_in_time",
                            "value": {"type": "time", "year": 1942},
                        }
                    ],
                    "source": source,
                },  # fmt: skip
            ],
            "entities": [
                {"id": "person-1", "label": PERSON_LABEL, "kind": "human"},
                {"id": "aunt-1", "label": "Integration Aunt", "kind": "human"},
                {
                    "id": "event-1",
                    "label": "Deportation of Integration Test Person",
                    "kind": "deportation",
                },  # fmt: skip
            ],
        }
    )


def test_adr0014_vocabulary_writes(client, artifact):
    """ADR-0014: sex_or_gender vocab item, the relative+kinship_type
    fallback qualifier, and a deportation event all write and round-trip."""
    writer = BatchWriter(client)
    result = writer.write_batch(kinship_batch(artifact.artifact_qid))
    person_qid = result.created_items["person-1"]
    properties, items = client.vocabulary()

    # sex_or_gender points at the `male` vocab item
    sex = client.api_get(
        {
            "action": "wbgetclaims",
            "entity": person_qid,
            "property": properties["sex_or_gender"],
        }
    )
    assert items["male"] in {
        c["mainsnak"]["datavalue"]["value"]["id"]
        for cs in sex["claims"].values()
        for c in cs
    }

    # `relative` runs from the aunt to the ego and carries the verbatim
    # kinship_type qualifier
    aunt_qid = result.created_items["aunt-1"]
    rel = client.api_get({"action": "wbgetclaims", "entity": aunt_qid,
                          "property": properties["relative"]})  # fmt: skip
    (claim,) = [c for cs in rel["claims"].values() for c in cs]
    assert claim["mainsnak"]["datavalue"]["value"]["id"] == person_qid
    quals = claim.get("qualifiers", {}).get(properties["kinship_type"], [])
    assert any(q["datavalue"]["value"] == "maternal aunt" for q in quals)

    # the deportation event is an instance of the deportation subclass
    event_qid = result.created_items["event-1"]
    ev = client.api_get({"action": "wbgetclaims", "entity": event_qid,
                         "property": properties["instance_of"]})  # fmt: skip
    assert items["deportation"] in {
        c["mainsnak"]["datavalue"]["value"]["id"]
        for cs in ev["claims"].values()
        for c in cs
    }


# -- a minted item's type statements, and the two queues (#88) -------------
#
# A slice of the example universe (tests/fixtures/example_universe.py): the
# family firm as two of its sources give it. Read alone, the 2002 interview
# ("The hat shop, the Blue Hat") gives no kind that fits, so its extraction
# holds the firm as an unclassified entity whose type hint keeps the
# source's word (ADR-0013). A later run over the 1912 directory types the
# firm an organization, with an `instance of` claim of its own. The first
# batch mints the item, so the writer writes its kind and type hint.
#
# Every label carries a marker, so the fixture never reuses an item that
# another test or run wrote, and a re-run reuses and skips everything.

UNIVERSE = u.by_key()
INTERVIEW = "interview-2002"
DIRECTORY = "directory-1912"
TYPE_MARK = "(type-statement test)"
TYPE_HINT = "hat shop"  # the interview's own word for the firm
# the directory's entry: a firm, its founding year and its owner
FIRM_ENTRY = "Hutmacherei zum Blauen Hut, gegr. 1863, Inh. Sebastian Löbig"
# fixed, so a re-run's batches keep their fingerprints and reuse their
# extraction-run items
TYPE_RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-27T00:00:00+00:00",
}


def type_marked(key: str) -> str:
    """The universe's label (or title) for `key`, with this section's
    marker: a label no other test or run writes."""
    entry = UNIVERSE[key]
    return f"{getattr(entry, 'title', None) or entry.label} {TYPE_MARK}"


FIRM = type_marked("zum-blauen-hut")


def firm_says(source: str, prop: str) -> u.Assertion:
    """What `source` says about the firm's `prop` in the universe."""
    (assertion,) = [
        a for a in u.about("zum-blauen-hut", prop) if a.source == source
    ]
    return assertion


def firm_batches(sources: dict[str, str]) -> dict[str, ClaimBatch]:
    """Each source's batch about the firm, by universe source key;
    `sources` maps those keys to artifact QIDs."""

    def batch(source, kind, claims, type_hint=None) -> ClaimBatch:
        firm = {
            "kind": "local",
            "local_id": "firm",
            "label": FIRM,
            "instance_of": kind,
        }
        entity = {"id": "firm", "label": FIRM, "kind": kind}
        if type_hint:
            entity["type_hint"] = type_hint
        return ClaimBatch.model_validate(
            {
                "extraction_run": TYPE_RUN,
                "claims": [
                    {
                        "subject": firm,
                        "property": prop,
                        "value": value,
                        "source": {
                            "artifact": {
                                "kind": "qid",
                                "qid": sources[source],
                            },
                            "transcription_excerpt": {
                                "text": excerpt,
                                "language": UNIVERSE[source].lang,
                            },
                        },
                    }
                    for prop, value, excerpt in claims
                ],
                "entities": [entity],
            }
        )

    name = firm_says(INTERVIEW, "referred_to_as")
    name_value = {
        "type": "monolingual",
        "value": {"text": name.value, "language": UNIVERSE[INTERVIEW].lang},
    }
    founded = firm_says(DIRECTORY, "start_time")
    founded_value = {"type": "time", "year": founded.value.year}
    organization = {
        "type": "item",
        "value": {"kind": "vocab", "key": "organization"},
    }
    return {
        INTERVIEW: batch(
            INTERVIEW,
            "unclassified_entity",
            [("referred_to_as", name_value, name.excerpt)],
            type_hint=TYPE_HINT,
        ),
        DIRECTORY: batch(
            DIRECTORY,
            "organization",
            [
                ("instance_of", organization, FIRM_ENTRY),
                ("start_time", founded_value, founded.excerpt),
            ],
        ),
    }


@dataclass(frozen=True)
class TypedFirm:
    firm: str  # the firm's QID
    sources: dict[str, str]  # universe source key -> artifact QID
    runs: dict[str, str]  # universe source key -> extraction-run QID
    batches: dict[str, ClaimBatch]  # universe source key -> its batch


@pytest.fixture(scope="module")
def typed_firm(client, config) -> TypedFirm:
    """Write the slice, the interview's batch first (idempotent: a re-run
    reuses and skips everything), and wait until the query service holds
    it."""
    # quoted, never paraphrased
    assert TYPE_HINT in UNIVERSE[INTERVIEW].text
    assert FIRM_ENTRY in UNIVERSE[DIRECTORY].text
    sources = {
        key: register_artifact(
            client,
            media_path=None,
            artifact_type=UNIVERSE[key].artifact_class or "artifact",
            title=type_marked(key),
            description="written by tests/integration; safe to delete",
        ).artifact_qid
        for key in (INTERVIEW, DIRECTORY)
    }
    batches = firm_batches(sources)
    results = {
        key: BatchWriter(client).write_batch(batches[key])
        for key in (INTERVIEW, DIRECTORY)
    }
    firm = results[INTERVIEW].created_items["firm"]
    assert results[DIRECTORY].created_items["firm"] == firm  # one item
    runs = {key: result.run_qid for key, result in results.items()}
    wait_for_query_service(
        config, client, [firm, *sources.values(), *runs.values()]
    )
    return TypedFirm(firm, sources, runs, batches)


def statements_by_key(client, qid: str) -> dict[str, list[dict]]:
    """The item's statements, by property key (API, lag-free)."""
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    data = client.api_get({"action": "wbgetclaims", "entity": qid})
    return {key_of[pid]: claims for pid, claims in data["claims"].items()}


def item_value(statement: dict) -> str:
    return statement["mainsnak"]["datavalue"]["value"]["id"]


def cited(client, statement: dict) -> list[set[tuple[str, str]]]:
    """Each reference block, as the (property key, item) pairs it cites."""
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    return [
        {
            (key_of[pid], snak["datavalue"]["value"]["id"])
            for pid, snaks in reference["snaks"].items()
            for snak in snaks
            if snak["datavalue"]["type"] == "wikibase-entityid"
        }
        for reference in statement["references"]
    ]


def statement_uri(config, guid: str) -> str:
    """API statement GUID (``Q5$uuid``) -> its RDF node URI."""
    return f"{config.concept_uri}/entity/statement/{guid.replace('$', '-', 1)}"


def rows_about(client, config, query: str, var: str, qids) -> list[dict]:
    """The rows of packaged query `query` whose `var` is one of `qids`.
    Rows about anything else on the instance are dropped before any
    assertion or failure report can show them."""
    properties, items = client.vocabulary()
    rendered = render_for_instance(
        load_templates()[query], properties, items, config.concept_uri
    )
    wanted = {f"{config.concept_uri}/entity/{qid}" for qid in qids}
    return [
        row
        for row in run_sparql(config, rendered)
        if row[var]["value"] in wanted
    ]


def test_minted_type_statements_cite_artifact_and_run(client, typed_firm):
    """Rules 1 and 2 (ADR-0003): the kind and type hint the writer wrote
    when it minted the firm are stated in the interview and, in a second
    reference, inferred from the interview's extraction run, the shape
    the directory's own `instance of` claim has."""
    _, items = client.vocabulary()
    statements = statements_by_key(client, typed_firm.firm)
    kinds = {item_value(s): s for s in statements["instance_of"]}
    assert set(kinds) == {items["unclassified_entity"], items["organization"]}
    (hint,) = statements["type_hint"]
    assert hint["mainsnak"]["datavalue"]["value"] == TYPE_HINT

    for statement, source in (
        (kinds[items["unclassified_entity"]], INTERVIEW),  # minted
        (hint, INTERVIEW),  # minted
        (kinds[items["organization"]], DIRECTORY),  # claimed
    ):
        assert statement["rank"] == "normal"
        assert cited(client, statement) == [
            {("stated_in", typed_firm.sources[source])},
            {("inferred_from_extraction_run", typed_firm.runs[source])},
        ]


def test_queue_lists_every_type_statement(client, config, typed_firm):
    """ADR-0023 §5: each of the firm's statements is a machine statement
    with one source and no curator sibling, so the reappraisal queue lists
    all five, one row each. The minted kind is not a curator witness for
    the directory's later `instance of`: both are listed. The artifacts'
    and run items' own type statements are not machine writes, and none
    is listed."""
    statements = statements_by_key(client, typed_firm.firm)
    # two kinds, the type hint, the interview's name for the firm and the
    # directory's founding year
    assert {key: len(s) for key, s in statements.items()} == {
        "instance_of": 2,
        "type_hint": 1,
        "referred_to_as": 1,
        "start_time": 1,
    }
    expected = sorted(
        statement_uri(config, s["id"])
        for claims in statements.values()
        for s in claims
    )
    queue = "under-witnessed-statements"
    rows = rows_about(client, config, queue, "subject", [typed_firm.firm])
    assert sorted(row["statement"]["value"] for row in rows) == expected

    others = [*typed_firm.sources.values(), *typed_firm.runs.values()]
    assert rows_about(client, config, queue, "subject", others) == []


def test_no_statement_of_the_slice_is_unreferenced(client, config, typed_firm):
    """ADR-0003 §3: the unreferenced report lists none of the slice's
    statements, whether the writer or the registrar wrote them."""
    qids = [
        typed_firm.firm,
        *typed_firm.sources.values(),
        *typed_firm.runs.values(),
    ]
    report = "unreferenced-statements"
    assert rows_about(client, config, report, "entity", qids) == []


def test_rewriting_the_slice_writes_nothing(client, typed_firm):
    """A re-run reuses the firm and both run items: no second kind or type
    hint, and no claim written again."""
    for key, claims in ((INTERVIEW, 1), (DIRECTORY, 2)):
        again = BatchWriter(client).write_batch(typed_firm.batches[key])
        assert again.created_items["firm"] == typed_firm.firm
        assert again.run_qid == typed_firm.runs[key]
        assert again.written == again.referenced == 0
        assert again.skipped == claims
    statements = statements_by_key(client, typed_firm.firm)
    assert len(statements["instance_of"]) == 2
    assert len(statements["type_hint"]) == 1


# -- a later source adds to an existing item's kind (#233) ------------------
#
# Another slice of the example universe, under a marker of its own. The
# 1905 marriage certificate's batch mints Sebastian Löbig (human) and his
# occupation, Hatter. The 2002 interview's batch mints the family firm
# under the interview's name for it, "the Blue Hat", as an unclassified
# entity with the type hint "hat shop" (ADR-0013). The 1912 directory's
# batch then names Sebastian ("Inh. Sebastian Löbig", the firm's owner)
# and the firm, which it types an organization under its full name.
# Before it is written it takes the steps a run takes: a reviewer's
# approved equivalence joins its firm to the interview's item, and the
# resolve step matches Sebastian by exact label, so its claims name both
# by QID (ADR-0007).
#
# The marker keeps this slice apart from every other test's items, and the
# fixed run metadata keeps each batch's fingerprint, so a re-run reuses
# the items and run items and writes nothing.

WITNESS_MARK = "(kind-witness test)"
MARRIAGE = "marriage-cert-1905"
WITNESS_RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-27T12:00:00+00:00",
}


def witness_marked(label: str) -> str:
    return f"{label} {WITNESS_MARK}"


def says(subject: str, source: str, prop: str) -> u.Assertion:
    """What `source` says about `subject`'s `prop` in the universe."""
    (assertion,) = [a for a in u.about(subject, prop) if a.source == source]
    return assertion


def entity_ref(entity: dict) -> dict:
    """A claim's local ref to a manifest entity, as extraction writes it:
    with the manifest's canonical label and kind."""
    return {
        "kind": "local",
        "local_id": entity["id"],
        "label": entity["label"],
        "instance_of": entity["kind"],
    }


def witness_batch(
    source: str, sources: dict[str, str], entities: list, claims: list
) -> ClaimBatch:
    """`source`'s batch: its manifest, and its claims as (subject entity,
    property, value, excerpt, qualifiers)."""
    return ClaimBatch.model_validate(
        {
            "extraction_run": WITNESS_RUN,
            "claims": [
                {
                    "subject": entity_ref(subject),
                    "property": prop,
                    "value": value,
                    "qualifiers": qualifiers,
                    "source": {
                        "artifact": {"kind": "qid", "qid": sources[source]},
                        "transcription_excerpt": {
                            "text": excerpt,
                            "language": UNIVERSE[source].lang,
                        },
                    },
                }
                for subject, prop, value, excerpt, qualifiers in claims
            ],
            "entities": entities,
        }
    )


def witness_batches(sources: dict[str, str]) -> dict[str, ClaimBatch]:
    """Each source's batch, by universe source key, as extraction gives
    it (the directory's before resolution)."""
    occupation = says("sebastian-loebig", MARRIAGE, "occupation")
    name = says("zum-blauen-hut", INTERVIEW, "referred_to_as")
    owner = says("sebastian-loebig", DIRECTORY, "employer")
    founded = says("zum-blauen-hut", DIRECTORY, "start_time")
    sebastian = {
        "id": "sebastian",
        "label": witness_marked(UNIVERSE["sebastian-loebig"].label),
        "kind": "human",
    }
    hatter = {
        "id": "hatter",
        "label": witness_marked(UNIVERSE["hatter"].label),
        "kind": "occupation_class",
    }
    blue_hat = {
        "id": "firm",
        "label": witness_marked(name.value),
        "kind": "unclassified_entity",
        "type_hint": TYPE_HINT,
    }
    firm = {
        "id": "firm",
        "label": witness_marked(UNIVERSE["zum-blauen-hut"].label),
        "kind": "organization",
    }

    def item(entity: dict) -> dict:
        return {"type": "item", "value": entity_ref(entity)}

    def year(date: u.Date) -> dict:
        return {"type": "time", "year": date.year}

    owned_in = dict(owner.qualifiers)["point_in_time"]
    return {
        MARRIAGE: witness_batch(
            MARRIAGE,
            sources,
            [sebastian, hatter],
            [(sebastian, "occupation", item(hatter), occupation.excerpt, [])],
        ),
        INTERVIEW: witness_batch(
            INTERVIEW,
            sources,
            [blue_hat],
            [
                (
                    blue_hat,
                    "referred_to_as",
                    {
                        "type": "monolingual",
                        "value": {
                            "text": name.value,
                            "language": UNIVERSE[INTERVIEW].lang,
                        },
                    },
                    name.excerpt,
                    [],
                )
            ],
        ),
        DIRECTORY: witness_batch(
            DIRECTORY,
            sources,
            [sebastian, firm],
            [
                (
                    sebastian,
                    "employer",
                    item(firm),
                    owner.excerpt,
                    [{"property": "point_in_time", "value": year(owned_in)}],
                ),
                (firm, "start_time", year(founded.value), founded.excerpt, []),
            ],
        ),
    }


@dataclass(frozen=True)
class WitnessedSlice:
    items: dict[str, str]  # "sebastian", "hatter", "firm" -> QID
    sources: dict[str, str]  # universe source key -> artifact QID
    runs: dict[str, str]  # universe source key -> extraction-run QID
    batches: dict[str, ClaimBatch]  # as written: the directory's resolved


@pytest.fixture(scope="module")
def witnessed(client, config) -> WitnessedSlice:
    """Write the slice in source order, the directory's batch after the
    equivalence and resolve steps, and wait until the query service holds
    it. Idempotent: a re-run reuses and skips everything."""
    for key, quoted in (
        (MARRIAGE, "der Hutmacher Sebastian Löbig"),
        (INTERVIEW, "The hat shop, the Blue Hat"),
        (DIRECTORY, "Inh. Sebastian Löbig"),
    ):
        assert quoted in UNIVERSE[key].text  # quoted, never paraphrased
    sources = {
        key: register_artifact(
            client,
            media_path=None,
            artifact_type=UNIVERSE[key].artifact_class or "artifact",
            title=witness_marked(UNIVERSE[key].title),
            description="written by tests/integration; safe to delete",
        ).artifact_qid
        for key in (MARRIAGE, INTERVIEW, DIRECTORY)
    }
    batches = witness_batches(sources)
    results = {
        key: BatchWriter(client).write_batch(batches[key])
        for key in (MARRIAGE, INTERVIEW)
    }
    items = {
        "sebastian": results[MARRIAGE].created_items["sebastian"],
        "hatter": results[MARRIAGE].created_items["hatter"],
        "firm": results[INTERVIEW].created_items["firm"],
    }
    approved = apply_equivalences(batches[DIRECTORY], {"firm": items["firm"]})
    _, item_labels = client.label_index()
    batches[DIRECTORY] = resolve_batch(approved, item_labels)
    # the writer sees QIDs only: no local ref of the directory's is left
    assert [claim.subject for claim in batches[DIRECTORY].claims] == [
        QidRef(qid=items["sebastian"]),
        QidRef(qid=items["firm"]),
    ]
    assert batches[DIRECTORY].claims[0].value.value == QidRef(
        qid=items["firm"]
    )
    results[DIRECTORY] = BatchWriter(client).write_batch(batches[DIRECTORY])
    runs = {key: result.run_qid for key, result in results.items()}
    wait_for_query_service(
        config,
        client,
        [*items.values(), *sources.values(), *runs.values()],
    )
    return WitnessedSlice(items, sources, runs, batches)


def test_later_source_adds_its_references_to_a_kind(client, witnessed):
    """The directory names Sebastian, whom the marriage certificate's
    batch minted a human: his one `instance of` now cites both sources,
    each beside its extraction run (rules 1 and 2, ADR-0003). Hatter,
    which the directory does not name, keeps its one source."""
    _, items = client.vocabulary()
    sebastian = statements_by_key(client, witnessed.items["sebastian"])
    (kind,) = sebastian["instance_of"]
    assert item_value(kind) == items["human"]
    assert kind["rank"] == "normal"
    assert cited(client, kind) == [
        {("stated_in", witnessed.sources[MARRIAGE])},
        {("inferred_from_extraction_run", witnessed.runs[MARRIAGE])},
        {("stated_in", witnessed.sources[DIRECTORY])},
        {("inferred_from_extraction_run", witnessed.runs[DIRECTORY])},
    ]
    hatter = statements_by_key(client, witnessed.items["hatter"])
    (occupation_kind,) = hatter["instance_of"]
    assert cited(client, occupation_kind) == [
        {("stated_in", witnessed.sources[MARRIAGE])},
        {("inferred_from_extraction_run", witnessed.runs[MARRIAGE])},
    ]


def test_later_source_kind_coexists(client, witnessed):
    """The directory types the firm an organization, which the interview
    held unclassified with the type hint "hat shop". Both kinds stand at
    normal rank, each with its own source and run (ADR-0002), and the type
    hint is as the interview's batch wrote it."""
    _, items = client.vocabulary()
    statements = statements_by_key(client, witnessed.items["firm"])
    kinds = {item_value(s): s for s in statements["instance_of"]}
    assert set(kinds) == {items["unclassified_entity"], items["organization"]}
    (hint,) = statements["type_hint"]
    assert hint["mainsnak"]["datavalue"]["value"] == TYPE_HINT
    for statement, source in (
        (kinds[items["unclassified_entity"]], INTERVIEW),
        (hint, INTERVIEW),
        (kinds[items["organization"]], DIRECTORY),
    ):
        assert statement["rank"] == "normal"
        assert cited(client, statement) == [
            {("stated_in", witnessed.sources[source])},
            {("inferred_from_extraction_run", witnessed.runs[source])},
        ]


def test_queue_reflects_the_added_witness(client, config, witnessed):
    """ADR-0023 §5: Sebastian's kind has two sources now, so the
    under-witnessed queue no longer lists it. Every other statement of the
    slice has one source and no curator sibling, and is listed, one row
    each: the firm's two kinds among them."""
    statements = {
        name: statements_by_key(client, qid)
        for name, qid in witnessed.items.items()
    }
    assert {
        name: {key: len(claims) for key, claims in by_key.items()}
        for name, by_key in statements.items()
    } == {
        "sebastian": {"instance_of": 1, "occupation": 1, "employer": 1},
        "hatter": {"instance_of": 1},
        "firm": {
            "instance_of": 2,
            "type_hint": 1,
            "referred_to_as": 1,
            "start_time": 1,
        },
    }
    (witnessed_kind,) = statements["sebastian"]["instance_of"]
    expected = sorted(
        statement_uri(config, s["id"])
        for by_key in statements.values()
        for claims in by_key.values()
        for s in claims
        if s["id"] != witnessed_kind["id"]
    )
    rows = rows_about(
        client,
        config,
        "under-witnessed-statements",
        "subject",
        witnessed.items.values(),
    )
    assert sorted(row["statement"]["value"] for row in rows) == expected


def test_rewriting_the_later_slice_adds_nothing(client, witnessed):
    """A re-run of all three batches reuses every item and run item, and
    adds no statement and no reference block."""
    before = {
        qid: statements_by_key(client, qid) for qid in witnessed.items.values()
    }
    for key, claims in ((MARRIAGE, 1), (INTERVIEW, 1), (DIRECTORY, 2)):
        again = BatchWriter(client).write_batch(witnessed.batches[key])
        assert again.run_qid == witnessed.runs[key]
        assert (again.written, again.referenced, again.skipped) == (
            0,
            0,
            claims,
        )
        assert again.type_statements_written == 0
        assert again.type_statements_referenced == 0
    after = {
        qid: statements_by_key(client, qid) for qid in witnessed.items.values()
    }
    assert after == before
