"""Live pipeline flow: registration, a second source agreeing with the
first, whose reference joins the one statement, the item-10 vocabulary,
a minted item's type statements (#88), and a later source's kind for an
item that already exists (#233). Every claim cites its artifact by the
SHA-1 of the artifact's media (#102), which the writer resolves to the
registered item.

Every label, title and image carries the session's marker
(conftest.fixture_mark), so each session meets the fixture as a first
write: what an earlier session wrote, a live mutant's run included, never
stands on its items. Within the session a re-run finds its own items
again."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.identity import corpus, write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch, QidRef
from yizkr.pipeline import reconcile, registrar, resolve, writer
from yizkr.pipeline.reconcile import apply_equivalences
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.writer import BatchWriter

from .conftest import (
    fictional_png,
    fixture_mark,
    register_fictional,
    wait_for_query_service,
)

pytestmark = pytest.mark.live

# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#88, #102, #233: a certificate and its test person, agreeing "
    "births, the family firm as the 2002 interview and the 1912 "
    "directory give it, and Sebastian Löbig in the 1905 marriage "
    "certificate"
)
MARK = fixture_mark(FIXTURE, writer, registrar, resolve, reconcile)
ARTIFACT_TITLE = f"Integration-test certificate {MARK}"
PERSON_LABEL = f"Integration Test Person {MARK}"
# the certificate's media: bytes of the session's own
CERTIFICATE_BYTES = fictional_png(f"yizkr live test: certificate {MARK}")


def tiny_png(tmp_path):
    path = tmp_path / f"integration-test-certificate-{MARK}.png"
    path.write_bytes(CERTIFICATE_BYTES)
    return path


# the certificate the `artifact` fixture registers, by the SHA-1 of its
# media: the key its claims cite
CERTIFICATE = ArtifactRef.from_bytes(CERTIFICATE_BYTES)


def birth_batch(artifact: ArtifactRef, year: int) -> ClaimBatch:
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
                        "artifact": artifact.model_dump(mode="json"),
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
    second = register_fictional(
        client, f"{ARTIFACT_TITLE} (second source)", "certificate"
    )
    # the identity map of the two sources, in which their person is one
    # entity (#109)
    identity = corpus(
        birth_batch(CERTIFICATE, 1901),
        birth_batch(second.ref, 1901),
        entities=(PERSON_LABEL,),
    )
    # the first source's statement, written here
    write_mapped(
        BatchWriter(client), birth_batch(CERTIFICATE, 1901), identity=identity
    )
    writer = BatchWriter(client)
    result = write_mapped(
        writer, birth_batch(second.ref, 1901), identity=identity
    )
    assert result.written == 0
    # the session's first write of the second source appends its reference
    assert (result.referenced, result.skipped) == (1, 0)
    person_qid = result.created_items["person-1"]

    count, sources = stated_in_values(
        client, person_qid, "+1901-00-00T00:00:00Z"
    )
    assert count == 1  # still one statement, not a duplicate
    assert sources == {artifact.artifact_qid, second.qid}

    # re-running the same batch is a no-op (reference already present)
    rerun = write_mapped(
        BatchWriter(client), birth_batch(second.ref, 1901), identity=identity
    )
    assert rerun.written == 0
    assert rerun.referenced == 0
    assert rerun.skipped == 1


def vocabulary_batch(artifact: ArtifactRef) -> ClaimBatch:
    """One claim per item-10 vocabulary addition (quantity, monolingual
    cause, occupation item, burial place)."""
    subject = {
        "kind": "local",
        "local_id": "person-1",
        "label": PERSON_LABEL,
        "instance_of": "human",
    }
    source = {
        "artifact": artifact.model_dump(mode="json"),
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
                            "label": f"Integration test occupation {MARK}",
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
                            "label": f"Integration Test Cemetery {MARK}",
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
    first = write_mapped(writer, vocabulary_batch(CERTIFICATE))
    person_qid = first.created_items["person-1"]
    occupation_qid = first.created_items["occ-1"]

    rerun = write_mapped(BatchWriter(client), vocabulary_batch(CERTIFICATE))
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


# -- a minted item's type statements (#88) ---------------------------------
#
# A slice of the example universe (tests/fixtures/example_universe.py): the
# family firm as two of its sources give it. Read alone, the 2002 interview
# ("The hat shop, the Blue Hat") gives no kind that fits, so its extraction
# holds the firm as an unclassified entity whose type hint keeps the
# source's word (ADR-0013). A later run over the 1912 directory types the
# firm an organization, with an `instance of` claim of its own. The first
# batch mints the item, so the writer writes its kind and type hint.
#
# Every label carries a marker, with the module's (MARK), so the fixture
# never reuses an item that another test or an earlier session wrote, and
# a re-run within the session reuses and skips everything.

UNIVERSE = u.by_key()
INTERVIEW = "interview-2002"
DIRECTORY = "directory-1912"
TYPE_MARK = f"(type-statement test {MARK})"
TYPE_HINT = "hat shop"  # the interview's own word for the firm
# the directory's entry: a firm, its founding year and its owner
FIRM_ENTRY = "Hutmacherei zum Blauen Hut, gegr. 1863, Inh. Sebastian Löbig"
# fixed, so a re-run's batches within the session keep their fingerprints
# and reuse their extraction-run items
TYPE_RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-27T00:00:00+00:00",
}


def type_marked(key: str) -> str:
    """The universe's label (or title) for `key`, with this section's
    marker: a label no other test or session writes."""
    entry = UNIVERSE[key]
    return f"{getattr(entry, 'title', None) or entry.label} {TYPE_MARK}"


FIRM = type_marked("zum-blauen-hut")


def firm_says(source: str, prop: str) -> u.Assertion:
    """What `source` says about the firm's `prop` in the universe."""
    (assertion,) = [
        a for a in u.about("zum-blauen-hut", prop) if a.source == source
    ]
    return assertion


def firm_batches(sources: dict[str, ArtifactRef]) -> dict[str, ClaimBatch]:
    """Each source's batch about the firm, by universe source key;
    `sources` maps those keys to the keys the artifacts are cited by."""

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
                            "artifact": sources[source].model_dump(
                                mode="json"
                            ),
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


@pytest.fixture(scope="module")
def typed_firm(client, config) -> TypedFirm:
    """Write the slice, the interview's batch first (idempotent: a re-run
    within the session reuses and skips everything), and wait until the
    query service holds it."""
    # quoted, never paraphrased
    assert TYPE_HINT in UNIVERSE[INTERVIEW].text
    assert FIRM_ENTRY in UNIVERSE[DIRECTORY].text
    registered = {
        key: register_fictional(
            client,
            type_marked(key),
            UNIVERSE[key].artifact_class or "artifact",
        )
        for key in (INTERVIEW, DIRECTORY)
    }
    sources = {key: artifact.qid for key, artifact in registered.items()}
    batches = firm_batches(
        {key: artifact.ref for key, artifact in registered.items()}
    )
    identity = corpus(*batches.values(), entities=(FIRM,))
    results = {
        key: write_mapped(BatchWriter(client), batches[key], identity=identity)
        for key in (INTERVIEW, DIRECTORY)
    }
    firm = results[INTERVIEW].created_items["firm"]
    assert results[DIRECTORY].created_items["firm"] == firm  # one item
    runs = {key: result.run_qid for key, result in results.items()}
    wait_for_query_service(
        config, client, [firm, *sources.values(), *runs.values()]
    )
    return TypedFirm(firm, sources, runs)


def statements_by_key(client, qid: str) -> dict[str, list[dict]]:
    """The item's statements, by property key (API, lag-free)."""
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    data = client.api_get({"action": "wbgetclaims", "entity": qid})
    return {key_of[pid]: claims for pid, claims in data["claims"].items()}


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
# The marker, with the module's (MARK), keeps this slice apart from every
# other test's items and every earlier session's, and the fixed run
# metadata keeps each batch's fingerprint, so a re-run within the session
# reuses the items and run items and writes nothing.

WITNESS_MARK = f"(kind-witness test {MARK})"
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
    source: str,
    sources: dict[str, ArtifactRef],
    entities: list,
    claims: list,
) -> ClaimBatch:
    """`source`'s batch: its manifest, and its claims as (subject entity,
    property, value, excerpt, qualifiers), each citing `sources[source]`."""
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
                        "artifact": sources[source].model_dump(mode="json"),
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


def witness_batches(
    sources: dict[str, ArtifactRef],
) -> dict[str, ClaimBatch]:
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


@pytest.fixture(scope="module")
def witnessed(client, config) -> WitnessedSlice:
    """Write the slice in source order, the directory's batch after the
    equivalence and resolve steps, and wait until the query service holds
    it. Idempotent: a re-run within the session reuses and skips
    everything."""
    for key, quoted in (
        (MARRIAGE, "der Hutmacher Sebastian Löbig"),
        (INTERVIEW, "The hat shop, the Blue Hat"),
        (DIRECTORY, "Inh. Sebastian Löbig"),
    ):
        assert quoted in UNIVERSE[key].text  # quoted, never paraphrased
    registered = {
        key: register_fictional(
            client,
            witness_marked(UNIVERSE[key].title),
            UNIVERSE[key].artifact_class or "artifact",
        )
        for key in (MARRIAGE, INTERVIEW, DIRECTORY)
    }
    sources = {key: artifact.qid for key, artifact in registered.items()}
    batches = witness_batches(
        {key: artifact.ref for key, artifact in registered.items()}
    )
    identity = corpus(
        *batches.values(),
        entities=(witness_marked(UNIVERSE["sebastian-loebig"].label),),
    )
    results = {
        key: write_mapped(BatchWriter(client), batches[key], identity=identity)
        for key in (MARRIAGE, INTERVIEW)
    }
    items = {
        "sebastian": results[MARRIAGE].created_items["sebastian"],
        "hatter": results[MARRIAGE].created_items["hatter"],
        "firm": results[INTERVIEW].created_items["firm"],
    }
    approved = apply_equivalences(batches[DIRECTORY], {"firm": items["firm"]})
    batches[DIRECTORY] = resolve_batch(approved, identity)
    # the equivalence names the firm by its QID; Sebastian's ref stays
    # local, for the writer to find his item through the map
    assert batches[DIRECTORY].claims[1].subject == QidRef(qid=items["firm"])
    assert batches[DIRECTORY].claims[0].value.value == QidRef(
        qid=items["firm"]
    )
    results[DIRECTORY] = write_mapped(
        BatchWriter(client), batches[DIRECTORY], identity=identity
    )
    assert results[DIRECTORY].created_items == {
        "sebastian": items["sebastian"]
    }
    runs = {key: result.run_qid for key, result in results.items()}
    wait_for_query_service(
        config,
        client,
        [*items.values(), *sources.values(), *runs.values()],
    )
    return WitnessedSlice(items, sources, runs)
