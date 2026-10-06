"""Live check that a later document's aliases and kinds reach an item the
writer found rather than minted (#240, #109): through the identity map,
which puts every mention of Sebastian in one cluster and every mention
of the firm in another, from a batch with an entity manifest and from
one without, whose local refs give their entities' kinds.

A slice of the example universe, under a marker of its own. The 1905
marriage certificate's batch mints Sebastian Löbig, a human. The 2002
interview's batch mints the family firm, unclassified, with the type hint
"hat shop" (ADR-0013). The 1984 family tree names Sebastian again, and
"Wastl" too: its batch takes the resolve step, as a run's does, which
leaves its refs local, and the writer finds his item through the map.
The 1912 directory's batch is one the line-range tier made (ADR-0006):
no manifest, so its local refs type the firm an organization and
Sebastian a human, and the writer finds both through the map. Every
batch is written with the one map of the four.

The marker, eight hex digits of a hash over the fixture, over the
writer's and the resolve step's source and over eight random bytes drawn
once a session (conftest.fixture_mark), keeps the slice apart from every
other test's items and from every earlier session's, a live mutant's run
included, so each session meets it as a first write. The fixed run
metadata keeps each batch's fingerprint, so a re-run within the session
reuses the items and run items and writes nothing. Each source is
registered with media of its own, which its claims cite by SHA-1 (#102).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.fixtures import example_universe as u
from tests.fixtures.identity import corpus, write_mapped
from yizkr.claims import ArtifactRef, ClaimBatch
from yizkr.pipeline import resolve, writer
from yizkr.pipeline.clustering import IdentityMap
from yizkr.pipeline.resolve import resolve_batch
from yizkr.pipeline.writer import BatchWriter

from .conftest import fixture_mark, register_fictional

pytestmark = pytest.mark.live

UNIVERSE = u.by_key()
# what the fixture is; a change to it gives the fixture new items
FIXTURE = (
    "#240: Sebastian Löbig and the family firm, from the 1905 marriage "
    "certificate, the 2002 interview, the 1984 family tree and the 1912 "
    "directory"
)
MARK = f"(found-item test {fixture_mark(FIXTURE, writer, resolve)})"
MARRIAGE = "marriage-cert-1905"
DIRECTORY = "directory-1912"
TREE = "family-tree-1984"
INTERVIEW = "interview-2002"
# the order the sources are written in
SOURCES = (MARRIAGE, INTERVIEW, TREE, DIRECTORY)
RUN = {
    "model_identifier": "integration-test",
    "pipeline_version": "0.0.0",
    "started_at": "2026-09-28T12:00:00+00:00",
}
TYPE_HINT = "hat shop"  # the interview's own word for the firm
DIMINUTIVE = "Wastl"  # the tree's other name for Sebastian


def marked(label: str) -> str:
    return f"{label} {MARK}"


SEBASTIAN = marked(UNIVERSE["sebastian-loebig"].label)
FIRM = marked(UNIVERSE["zum-blauen-hut"].label)


def says(subject: str, source: str, prop: str) -> u.Assertion:
    """What `source` says about `subject`'s `prop` in the universe."""
    (assertion,) = [a for a in u.about(subject, prop) if a.source == source]
    return assertion


def ref(local_id: str, label: str, kind: str) -> dict:
    return {
        "kind": "local",
        "local_id": local_id,
        "label": label,
        "instance_of": kind,
    }


def year(value: int, month: int | None = None, day: int | None = None):
    date = {"type": "time", "year": value}
    if month:
        date["month"] = month
    if day:
        date["day"] = day
    return date


def batches(sources: dict[str, ArtifactRef]) -> dict[str, ClaimBatch]:
    """Each source's batch, by universe source key, as extraction gives
    it (the tree's before the resolve step), each claim citing its
    source's key in `sources`."""

    def claim(subject, prop, value, source, excerpt, qualifiers=()):
        return {
            "subject": subject,
            "property": prop,
            "value": value,
            "qualifiers": list(qualifiers),
            "source": {
                "artifact": sources[source].model_dump(mode="json"),
                "transcription_excerpt": {
                    "text": excerpt,
                    "language": UNIVERSE[source].lang,
                },
            },
        }

    def batch(claims: list, entities: list = ()) -> ClaimBatch:
        return ClaimBatch.model_validate(
            {
                "extraction_run": RUN,
                "claims": claims,
                "entities": list(entities),
            }
        )

    person = {"id": "sebastian", "label": SEBASTIAN, "kind": "human"}
    firm = {
        "id": "firm",
        "label": FIRM,
        "kind": "unclassified_entity",
        "type_hint": TYPE_HINT,
    }
    certificate_birth = says("sebastian-loebig", MARRIAGE, "date_of_birth")
    tree_birth = says("sebastian-loebig", TREE, "date_of_birth")
    name = says("zum-blauen-hut", INTERVIEW, "referred_to_as")
    owner = says("sebastian-loebig", DIRECTORY, "employer")
    founded = says("zum-blauen-hut", DIRECTORY, "start_time")
    # the line-range tier's refs: chunk-scoped ids, each with its kind
    listed_firm = ref("c001:firm", FIRM, "organization")
    listed_owner = ref("c001:owner", SEBASTIAN, "human")
    birth = certificate_birth.value
    owned_in = dict(owner.qualifiers)["point_in_time"]
    return {
        MARRIAGE: batch(
            [
                claim(
                    ref("sebastian", SEBASTIAN, "human"),
                    "date_of_birth",
                    year(birth.year, birth.month, birth.day),
                    MARRIAGE,
                    certificate_birth.excerpt,
                )
            ],
            [person],
        ),
        INTERVIEW: batch(
            [
                claim(
                    ref("firm", FIRM, "unclassified_entity"),
                    "referred_to_as",
                    {
                        "type": "monolingual",
                        "value": {
                            "text": name.value,
                            "language": UNIVERSE[INTERVIEW].lang,
                        },
                    },
                    INTERVIEW,
                    name.excerpt,
                )
            ],
            [firm],
        ),
        TREE: batch(
            [
                claim(
                    ref("sebastian", SEBASTIAN, "human"),
                    "date_of_birth",
                    year(tree_birth.value.year),
                    TREE,
                    tree_birth.excerpt,
                )
            ],
            [dict(person, aliases=[DIMINUTIVE])],
        ),
        DIRECTORY: batch(
            [
                claim(
                    listed_firm,
                    "start_time",
                    year(founded.value.year),
                    DIRECTORY,
                    founded.excerpt,
                ),
                claim(
                    listed_owner,
                    "employer",
                    {"type": "item", "value": listed_firm},
                    DIRECTORY,
                    owner.excerpt,
                    [
                        {
                            "property": "point_in_time",
                            "value": year(owned_in.year),
                        }
                    ],
                ),
            ]
        ),
    }


@dataclass(frozen=True)
class FoundSlice:
    items: dict[str, str]  # "sebastian", "firm" -> QID
    sources: dict[str, str]  # universe source key -> artifact QID
    runs: dict[str, str]  # universe source key -> extraction-run QID
    batches: dict[str, ClaimBatch]  # as written: the tree's resolved
    identity: IdentityMap  # the map every batch is written with


@pytest.fixture(scope="module")
def found(client) -> FoundSlice:
    """Write the slice in source order, the tree's batch after the resolve
    step. Idempotent: a re-run within the session reuses and skips
    everything."""
    for key, quoted in (
        (TREE, "Sebastian 'Wastl' 1862"),
        (INTERVIEW, "The hat shop, the Blue Hat"),
        (DIRECTORY, "gegr. 1863, Inh. Sebastian Löbig"),
    ):
        assert quoted in UNIVERSE[key].text  # quoted, never paraphrased
    registered = {
        key: register_fictional(
            client,
            marked(UNIVERSE[key].title),
            UNIVERSE[key].artifact_class or "artifact",
        )
        for key in SOURCES
    }
    sources = {key: artifact.qid for key, artifact in registered.items()}
    written = batches(
        {key: artifact.ref for key, artifact in registered.items()}
    )
    identity = corpus(*written.values(), entities=(SEBASTIAN, FIRM))
    results = {}
    for key in SOURCES:
        if key == TREE:
            written[TREE] = resolve_batch(written[TREE], identity)
        results[key] = write_mapped(
            BatchWriter(client), written[key], identity=identity
        )
    items = {
        "sebastian": results[MARRIAGE].created_items["sebastian"],
        "firm": results[INTERVIEW].created_items["firm"],
    }
    # the resolve step leaves the tree's refs local; the writer finds
    # Sebastian's item through the map
    assert results[TREE].created_items == {"sebastian": items["sebastian"]}
    # the directory's refs reach the writer, which finds both items
    assert results[DIRECTORY].created_items == {
        "c001:firm": items["firm"],
        "c001:owner": items["sebastian"],
    }
    runs = {key: result.run_qid for key, result in results.items()}
    return FoundSlice(items, sources, runs, written, identity)


def statements_by_key(client, qid: str) -> dict[str, list[dict]]:
    """The item's statements, by property key (API, lag-free)."""
    properties, _ = client.vocabulary()
    key_of = {pid: key for key, pid in properties.items()}
    data = client.api_get({"action": "wbgetclaims", "entity": qid})
    return {key_of[pid]: claims for pid, claims in data["claims"].items()}


def aliases(client, qid: str) -> list[str]:
    data = client.api_get(
        {"action": "wbgetentities", "ids": qid, "props": "aliases"}
    )
    entity = data["entities"][qid]
    return sorted(a["value"] for a in entity.get("aliases", {}).get("en", []))


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


def source_and_run(found: FoundSlice, key: str) -> list[set]:
    return [
        {("stated_in", found.sources[key])},
        {("inferred_from_extraction_run", found.runs[key])},
    ]


def test_resolved_record_gives_its_aliases(client, found):
    """The tree's record reached the writer with its refs local, and the
    map put it in Sebastian's cluster; his item has the tree's name for
    him, and his label is not an alias of it."""
    assert aliases(client, found.items["sebastian"]) == [DIMINUTIVE]


def test_every_later_source_witnesses_a_kind(client, found):
    """Sebastian's one `instance of` cites the certificate that minted him,
    then the tree (a record of his cluster) and the directory (a ref
    without a record), each beside its extraction run. The tree's kind
    was written after its aliases went on, and they are still on."""
    _, items = client.vocabulary()
    (kind,) = statements_by_key(client, found.items["sebastian"])[
        "instance_of"
    ]
    assert item_value(kind) == items["human"]
    assert kind["rank"] == "normal"
    assert cited(client, kind) == [
        *source_and_run(found, MARRIAGE),
        *source_and_run(found, TREE),
        *source_and_run(found, DIRECTORY),
    ]


def test_a_ref_without_a_record_gives_a_kind_that_coexists(client, found):
    """The directory's line-range batch types the firm an organization,
    which the interview held unclassified with a type hint: both kinds
    stand at normal rank, each with its own source and run (ADR-0002)."""
    _, items = client.vocabulary()
    statements = statements_by_key(client, found.items["firm"])
    kinds = {item_value(s): s for s in statements["instance_of"]}
    assert set(kinds) == {items["unclassified_entity"], items["organization"]}
    (hint,) = statements["type_hint"]
    for statement, source in (
        (kinds[items["unclassified_entity"]], INTERVIEW),
        (hint, INTERVIEW),
        (kinds[items["organization"]], DIRECTORY),
    ):
        assert statement["rank"] == "normal"
        assert cited(client, statement) == source_and_run(found, source)


def test_rewriting_the_slice_adds_nothing(client, found):
    """A re-run of every batch reuses every item and run item, and adds no
    statement, reference block or alias."""
    before = {
        qid: (statements_by_key(client, qid), aliases(client, qid))
        for qid in found.items.values()
    }
    for key in SOURCES:
        again = write_mapped(
            BatchWriter(client), found.batches[key], identity=found.identity
        )
        assert again.run_qid == found.runs[key]
        assert (again.written, again.referenced) == (0, 0)
        assert again.skipped == len(found.batches[key].claims)
        assert again.type_statements_written == 0
        assert again.type_statements_referenced == 0
    after = {
        qid: (statements_by_key(client, qid), aliases(client, qid))
        for qid in found.items.values()
    }
    assert after == before
