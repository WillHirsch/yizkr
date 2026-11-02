"""Verified legacy identity survives migration and graph-only provenance reads.

The historical map and readings are authored fictional inputs, not provider
outputs. Creation evidence, reviewed assignments, metadata writes, retries and
WDQS reads all cross the real isolated persistence boundary.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from wikibaseintegrator.datatypes import Item
from wikibaseintegrator.entities.item import ItemEntity
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures.identity import joined, mapped
from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import ArtifactRef, QidRef
from yizkr.core import sparql
from yizkr.core.identity_migration import MigrationRefused
from yizkr.core.provenance import RecordRef
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline.clustering import ADJUDICATED, IdentityMap, identity_hash
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewRequest,
    ReviewSource,
)
from yizkr.pipeline.run_provenance import read_record
from yizkr.pipeline.transcribe import Transcription
from yizkr.pipeline.writer import BatchWriter
from yizkr.queries.publish import load_templates, render_for_instance

from .test_write_query import _synchronize

pytestmark = pytest.mark.live
REVIEW_TIME = "2026-01-01T12:00:00Z"
SOURCES = ("birth-extract-1938", "naturalisation-1952")
NAMES = ("Gustav", "Gustave LOBIG")


class InterruptedMetadata(BaseException):
    """Stop after the real server accepts a partial metadata publication."""


def _entity(client, qid):
    return client.api_get(
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "labels|descriptions|aliases|claims",
        }
    )["entities"][qid]


def _method(identity, method):
    data = copy.deepcopy(identity.data)
    for value in data["mentions"].values():
        value["method"] = method
    for value in data["clusters"].values():
        value["method"] = method
    data["hash"] = identity_hash(data["mentions"])
    return IdentityMap(data)


def _seed(client, directory):
    """Create actual old cluster authority without claiming the lane's map.

    The lane also exercises a normal one-map write. This fixture uses the
    exact old cluster creation operation, and explicit-QID historical writes,
    so its private map neither reserves that global guard nor adds map-labelled
    extraction runs that could collide with another boundary case.
    """
    registry = CreationRegistry(client)
    properties, items = client.vocabulary()
    sources, registered = [], []
    for key, name in zip(SOURCES, NAMES, strict=True):
        case = source_case(key)
        path = directory / f"fictional-identity-migration-{key}.txt"
        path.write_bytes(
            case.source_bytes + b"\nFictional migration fixture.\n"
        )
        artifact = ArtifactRef.from_path(path)
        assert artifact.sha1 != case.sha1
        batch = birth_batch(key)
        batch.entities[0].label = name
        batch.entities[0].aliases = [name]
        batch.entities[0].mention_lines = [1]
        batch.claims[0].source = batch.claims[0].source.model_copy(
            update={"artifact": artifact}
        )
        reading = Transcription(
            artifact=artifact,
            text=path.read_text(),
            language=case.language,
            engine="authored-fictional-text",
        )
        sources.append(ReviewSource(artifact.sha1, batch, reading))
        registered.append(
            register_artifact(
                client,
                path,
                "certificate",
                "Migration fixture: " + case.title,
                pages=1,
            )
        )
    # An authored historical decision with absent original adjudication run
    # metadata exercises honest limitations, not a fictional provider call.
    identity = _method(
        joined(mapped(*(source.batch for source in sources)), NAMES),
        ADJUDICATED,
    )
    (cluster,) = identity.clusters()
    operation = {"kind": "cluster", "map": identity.hash, "cluster": cluster}
    item = client.wbi.item.new()
    item.labels.set("en", "Migration fixture: Gustav Löbig")
    item.descriptions.set("en", "fictional legacy map-created person")
    kind = Item(prop_nr=properties["instance_of"], value=items["human"])
    item.claims.add(kind)
    binding = registry.create(
        operation, item, summary="fictional legacy cluster"
    )
    runs = []
    for source in sources:
        explicit = source.batch.model_copy(deep=True)
        explicit.entities = []
        explicit.claims[0].subject = QidRef(qid=binding.qid)
        written = BatchWriter(
            client, record_dir=directory / "historical-process-records"
        ).write_batch(explicit)
        assert written.written == 1 and not written.refused
        runs.append(written.run_qid)
    edited = client.wbi.item.get(binding.qid)
    (kind,) = edited.claims.get(properties["instance_of"])
    for artifact, run in zip(registered, runs, strict=True):
        for property_key, qid in (
            ("stated_in", artifact.artifact_qid),
            ("inferred_from_extraction_run", run),
        ):
            reference = Reference()
            reference.add(Item(prop_nr=properties[property_key], value=qid))
            kind.references.add(reference)
    kind.rank = WikibaseRank.PREFERRED
    births = edited.claims.get(properties["date_of_birth"])
    assert len(births) == 2
    births[0].rank = WikibaseRank.DEPRECATED
    edited.labels.set("en", "Curator's migration fixture: Gustav Löbig")
    edited.descriptions.set("en", "fictional curator-preserved description")
    write_existing(
        edited, summary="fictional fixture: curator evidence and ranks"
    )
    binding = registry.lookup(operation)
    assert binding is not None
    return (
        identity,
        ReviewRequest(sources=tuple(sources)),
        tuple(
            MentionKey(artifact_sha1=source.artifact_sha1, local_id="gustav")
            for source in sources
        ),
        binding,
        _entity(client, binding.qid),
    )


def _approve(store, prepared):
    for proposal in prepared.review.proposals:
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional curator",
            time=REVIEW_TIME,
        )


def _assert_preserved(client, qid, before):
    after = _entity(client, qid)
    properties, _ = client.vocabulary()
    for field in ("labels", "descriptions", "aliases"):
        assert after.get(field, {}) == before.get(field, {})
    assert (
        after["claims"][properties["date_of_birth"]]
        == before["claims"][properties["date_of_birth"]]
    )
    (old_kind,) = before["claims"][properties["instance_of"]]
    (new_kind,) = after["claims"][properties["instance_of"]]
    assert new_kind["id"] == old_kind["id"]
    assert new_kind["rank"] == old_kind["rank"] == "preferred"
    references = {ref["hash"]: ref for ref in new_kind["references"]}
    for old in old_kind["references"]:
        assert references[old["hash"]] == old
    return after


def _authority_state(client):
    from yizkr.core.write_recovery import PREFIX

    return (
        set(client.list_entity_ids()),
        set(client.page_titles(PREFIX)),
        CreationRegistry(client).assignment_snapshot(),
    )


def test_verified_migration_preserves_evidence_and_publishes_graph_origins(
    fictional_client, tmp_path, monkeypatch
):
    from yizkr.pipeline.identity_migration import (
        accept_map_migration,
        prepare_map_migration,
    )
    from yizkr.pipeline.identity_provenance import publish_identity_provenance

    client = fictional_client
    identity, request, selections, binding, historical = _seed(
        client, tmp_path
    )
    registry = CreationRegistry(client)
    properties, items = client.vocabulary()
    before = _authority_state(client)

    # A valid map of the same names with no corresponding creation intent is
    # not evidence that a nearby item can be adopted by its current terms.
    with (
        ReviewStore(tmp_path / "missing-evidence.sqlite") as store,
        pytest.raises(MigrationRefused),
    ):
        prepare_map_migration(
            store,
            request,
            registry,
            _method(identity, "confident"),
            selections=selections,
        )
    assert _authority_state(client) == before

    # Two model records pointing to the same literal occurrence have no unique
    # correspondence. Choosing one local ID cannot silently resolve that fact.
    first = request.sources[0]
    ambiguous = first.batch.model_copy(deep=True)
    ambiguous.entities.append(
        ambiguous.entities[0].model_copy(update={"id": "other"})
    )
    unclear = replace(
        request,
        sources=(replace(first, batch=ambiguous), *request.sources[1:]),
    )
    with (
        ReviewStore(tmp_path / "ambiguous.sqlite") as store,
        pytest.raises((MigrationRefused, ReviewRefused)),
    ):
        prepare_map_migration(
            store, unclear, registry, identity, selections=selections
        )
    assert _authority_state(client) == before

    directory = tmp_path / "identity-records"
    path = tmp_path / "migration-review.sqlite"
    with ReviewStore(path) as store:
        prepared = prepare_map_migration(
            store, request, registry, identity, selections=selections
        )
        _approve(store, prepared)
        receipt = accept_map_migration(
            store, prepared, registry, record_dir=directory
        )
    assert len(receipt.correspondences) == len(receipt.assignments) == 2
    assert {c.binding.binding.entity_id for c in receipt.correspondences} == {
        binding.entity_id
    }
    assert {c.binding.binding.qid for c in receipt.correspondences} == {
        binding.qid
    }
    assert {c.method for c in receipt.correspondences} == {ADJUDICATED}
    assert registry.migration_receipt(receipt.migration_id) == receipt
    # Assignment/migration metadata has not applied or rewritten any claim.
    assert _entity(client, binding.qid) == historical
    record = read_record(directory, RecordRef(sha256=receipt.record_sha256))
    assert any(
        "model" in value and "prompt" in value and "unavailable" in value
        for value in record.limitations
    )
    assert record.unratified is None
    assert any("does not ratify" in value for value in record.limitations)
    assert all(
        not stage.model_identifier
        and not stage.model
        and not stage.prompt_hashes
        for stage in record.stages
    )

    graph_before = set(client.list_entity_ids())
    interrupted = []
    write = ItemEntity.write

    def interrupt_metadata(item, *args, **kwargs):
        written = write(item, *args, **kwargs)
        if written.id not in graph_before and any(
            claim.mainsnak.datavalue.get("value", {}).get("id")
            == items["identity_decision_record"]
            for claim in written.claims.get(properties["instance_of"]) or []
        ):
            interrupted.append(written.id)
            raise InterruptedMetadata()
        return written

    with monkeypatch.context() as patch:
        patch.setattr(ItemEntity, "write", interrupt_metadata)
        with pytest.raises(InterruptedMetadata):
            publish_identity_provenance(
                client, receipt.migration_id, record_dir=directory
            )
    assert len(interrupted) == 1
    assert set(client.list_entity_ids()) - graph_before == set(interrupted)
    assert _entity(client, binding.qid) == historical

    published = publish_identity_provenance(
        client, receipt.migration_id, record_dir=directory
    )
    assert published.record_qid == interrupted[0]
    assert published.changed
    projected = _assert_preserved(client, binding.qid, historical)
    (kind,) = projected["claims"][properties["instance_of"]]
    origins = [
        ref
        for ref in kind["references"]
        if properties["identity_method"] in ref["snaks"]
    ]
    confirmations = [
        ref
        for ref in kind["references"]
        if properties["identity_confirmation_actor"] in ref["snaks"]
    ]
    assert len(origins) == len(confirmations) == 2
    assert all(
        properties["identity_confirmation_actor"] not in ref["snaks"]
        for ref in origins
    )
    assert all(
        properties["identity_method"] not in ref["snaks"]
        for ref in confirmations
    )
    after = _authority_state(client)
    with ReviewStore(path) as store:
        repeated = prepare_map_migration(
            store, request, registry, identity, selections=selections
        )
        assert (
            accept_map_migration(
                store, repeated, registry, record_dir=directory
            )
            == receipt
        )
    assert not publish_identity_provenance(
        client, receipt.migration_id, record_dir=directory
    ).changed
    assert _authority_state(client) == after
    assert _entity(client, binding.qid) == projected

    _synchronize(client)
    query = render_for_instance(
        load_templates()["identity-provenance"],
        properties,
        items,
        client.config.concept_uri,
    )
    rows = [
        row
        for row in sparql.select(client.config.sparql_url, query)
        if row["migration"]["value"] == receipt.migration_id
    ]
    assert rows
    uri = client.config.concept_uri + "/entity/"
    assert {row["entity"]["value"] for row in rows} == {uri + binding.qid}
    assert {row["method"]["value"] for row in rows} == {
        uri + items["identity_adjudicated"]
    }
    assert {row["record"]["value"] for row in rows} == {
        uri + published.record_qid
    }
    assert {row["recordSha256"]["value"] for row in rows} == {
        receipt.record_sha256
    }
    assert {row["recordFile"]["value"] for row in rows} == {
        receipt.record_sha256 + ".json"
    }
    assert {row["assignmentRevision"]["value"] for row in rows} == {
        assignment.revision_id for assignment in receipt.assignments
    }
    assert {row["actor"]["value"] for row in rows} == {"fictional curator"}
    assert {row["confirmedAt"]["value"] for row in rows} == {REVIEW_TIME}
    assert len({row["decisionRevision"]["value"] for row in rows}) == 2
    assert not any("model" in row or "prompt" in row for row in rows)
    limits = {
        row["limitation"]["value"] for row in rows if "limitation" in row
    }
    assert any(
        "model" in value and "prompt" in value and "unavailable" in value
        for value in limits
    )
