"""Migration preserves creations and obtains ordinary current review."""

import copy
import json
from dataclasses import dataclass, replace

import pytest

from tests.unit.test_identity_review import reading
from tests.unit.test_pipeline import FakeWiki
from yizkr.core.identity_assignments import record_digest
from yizkr.core.identity_migration import MigrationRefused
from yizkr.core.provenance import ProcessRecord, RecordRef
from yizkr.core.write_recovery import (
    CreationRegistry,
    cluster_creation_operation,
    write_existing,
)
from yizkr.pipeline.clustering import (
    DOCUMENT,
    IdentityMap,
    Manifest,
    identity_hash,
)
from yizkr.pipeline.fingerprints import cluster_id, mention_fingerprint
from yizkr.pipeline.identity_migration import (
    accept_map_migration,
    prepare_map_migration,
    validated_map,
)
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewRequest,
    ReviewSource,
)
from yizkr.pipeline.run_provenance import read_record


@dataclass
class MigrationCase:
    wiki: object
    registry: CreationRegistry
    store: ReviewStore
    request: ReviewRequest
    identity: IdentityMap
    selections: tuple[MentionKey, ...]

    def prepare(self):
        return prepare_map_migration(
            self.store,
            self.request,
            self.registry,
            self.identity,
            selections=self.selections,
        )


@pytest.fixture
def migration_case(tmp_path):
    batch, text = reading()
    identity = IdentityMap.of([Manifest(text.artifact.sha1, batch, DOCUMENT)])
    wiki = FakeWiki(detached=True)
    registry = CreationRegistry(wiki)
    _, clusters = identity.clusters_of(batch, [text.artifact.sha1])
    for cid in set(clusters.values()):
        item = wiki.wbi.item.new()
        item.labels.set("en", identity.cluster(cid).labels[0])
        registry.create(
            cluster_creation_operation(identity.hash, cid),
            item,
            summary="fictional original cluster",
        )
    request = ReviewRequest(
        sources=(ReviewSource(text.artifact.sha1, batch, text),)
    )
    selections = tuple(
        MentionKey(artifact_sha1=text.artifact.sha1, local_id=key)
        for key in ("a", "b")
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        yield MigrationCase(
            wiki, registry, store, request, identity, selections
        )


def approve(case, prepared):
    for proposal in prepared.review.proposals:
        if proposal.operation == "assign":
            case.store.decide(
                proposal.id,
                proposal.revision,
                outcome="approve",
                actor="curator",
                time="2026-01-01T12:00:00+00:00",
                expected_digest=proposal.digest,
            )


def test_preparation_requires_ordinary_approval_and_preserves_graph(
    migration_case, tmp_path
):
    case = migration_case
    before = {
        key: item.get_json() for key, item in case.wiki.wbi.item.store.items()
    }
    pages = dict(case.wiki._recovery_pages)
    prepared = case.prepare()
    assert all(
        app.status == "needs_review"
        for app in prepared.review.projection.applications
    )
    assert case.wiki._recovery_pages == pages
    with pytest.raises(MigrationRefused, match="approval"):
        accept_map_migration(
            case.store,
            prepared,
            case.registry,
            record_dir=tmp_path / "records",
        )
    approve(case, prepared)
    accepted = accept_map_migration(
        case.store, prepared, case.registry, record_dir=tmp_path / "records"
    )
    assert len(accepted.assignments) == 2
    assert {
        key: item.get_json() for key, item in case.wiki.wbi.item.store.items()
    } == before
    assert all(
        case.wiki._recovery_pages[key] == page for key, page in pages.items()
    )
    assert accepted.kind == "identity-migration-accepted"
    record = read_record(
        tmp_path / "records", RecordRef(sha256=accepted.record_sha256)
    )
    payload = record.metadata["identity_migration"]
    assert payload["map"] == case.identity.data
    assert payload["request_sha256"] == accepted.migration_id
    assert len(payload["decisions"]) == 2
    assert all(d["actor"] == "curator" for d in payload["decisions"].values())
    assert any("producer model" in reason for reason in record.limitations)
    assert all(stage.model_identifier is None for stage in record.stages)
    assert not any(
        json.loads(page.text).get("kind") == "identity-map"
        for page in case.wiki._recovery_pages.values()
    )


def test_recovery_uses_receipt_after_curator_terms_and_unrelated_append(
    migration_case, tmp_path
):
    case = migration_case
    prepared = case.prepare()
    approve(case, prepared)
    records = tmp_path / "records"
    receipt = accept_map_migration(
        case.store, prepared, case.registry, record_dir=records
    )
    target = receipt.correspondences[0].binding.binding.qid
    item = case.wiki.wbi.item.get(target)
    item.labels.set("en", "Leopold, curator's chosen label")
    item.descriptions.set("en", "A retained fictional entity")
    write_existing(item, summary="fictional curator edit")
    other = prepare_map_migration(
        case.store,
        case.request,
        case.registry,
        case.identity,
        selections=(
            MentionKey(
                artifact_sha1=case.selections[0].artifact_sha1, local_id="c"
            ),
        ),
    )
    approve(case, other)
    accept_map_migration(case.store, other, case.registry, record_dir=records)
    pages = dict(case.wiki._recovery_pages)
    files = {p.name: p.read_bytes() for p in records.iterdir()}
    # Use the original bounded request; its review store also knows the later
    # assignment. That addition is not a change to the migrated scope.
    again = accept_map_migration(
        case.store, prepared, CreationRegistry(case.wiki), record_dir=records
    )
    assert again == receipt
    assert case.wiki._recovery_pages == pages
    assert {p.name: p.read_bytes() for p in records.iterdir()} == files


@pytest.mark.parametrize("failure", [ConnectionError, KeyboardInterrupt])
def test_lost_receipt_response_recovers_original_assignments(
    migration_case, tmp_path, monkeypatch, failure
):
    case = migration_case
    prepared = case.prepare()
    approve(case, prepared)
    create = case.wiki.create_page_once

    def interrupted(title, text, summary):
        result = create(title, text, summary)
        if "/migrations/" in title:
            raise failure("fictional receipt acknowledgement loss")
        return result

    monkeypatch.setattr(case.wiki, "create_page_once", interrupted)
    if failure is KeyboardInterrupt:
        with pytest.raises(KeyboardInterrupt):
            accept_map_migration(
                case.store,
                prepared,
                case.registry,
                record_dir=tmp_path / "records",
            )
    else:
        accept_map_migration(
            case.store,
            prepared,
            case.registry,
            record_dir=tmp_path / "records",
        )
    snapshot = case.registry.assignment_snapshot()
    receipt = accept_map_migration(
        case.store,
        prepared,
        CreationRegistry(case.wiki),
        record_dir=tmp_path / "records",
    )
    assert case.registry.assignment_snapshot() == snapshot
    assert len(snapshot.history) == 1
    assert receipt.assignment_head == snapshot.head


@pytest.mark.parametrize(
    "damage",
    [
        "version",
        "fingerprint",
        "membership",
        "method",
        "cluster_id",
        "provenance",
        "method_type",
        "non_json",
    ],
)
def test_inconsistent_or_unsupported_maps_refuse_before_effects(
    migration_case, damage
):
    case = migration_case
    data = copy.deepcopy(case.identity.data)
    fingerprint, row = next(iter(data["mentions"].items()))
    cid = row["cluster"]
    if damage == "version":
        data["version"] += 1
    elif damage == "fingerprint":
        row["label"] = "Different fictional occurrence"
    elif damage == "membership":
        data["clusters"][cid]["members"].append(fingerprint)
    elif damage == "method":
        data["clusters"][cid]["method"] = "owner_override"
    elif damage == "cluster_id":
        data["clusters"]["a" * 64] = data["clusters"].pop(cid)
    elif damage == "provenance":
        data["provenance"] = ProcessRecord(
            kind="clustering", metadata={"identity_map_hash": "a" * 64}
        ).model_dump(mode="json")
    elif damage == "method_type":
        row["method"] = []
    else:
        data["summary"]["unsupported"] = object()
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(MigrationRefused):
        prepare_map_migration(
            case.store,
            case.request,
            case.registry,
            IdentityMap(data),
            selections=case.selections,
        )
    assert case.wiki._recovery_pages == pages


def test_even_first_ordinal_of_repeated_old_components_refuses(migration_case):
    case = migration_case
    data = copy.deepcopy(case.identity.data)
    fingerprint, row = next(
        (key, value)
        for key, value in data["mentions"].items()
        if value["local_id"] == "a"
    )
    repeated = row | {"local_id": "another", "occurrence": 1}
    key = mention_fingerprint(
        repeated["document"],
        repeated["label"],
        repeated["kind"],
        repeated["span"],
        1,
    )
    cid = cluster_id([key])
    repeated["cluster"] = cid
    data["mentions"][key] = repeated
    data["clusters"][cid] = {
        "kind": repeated["kind"],
        "method": repeated["method"],
        "members": [key],
    }
    data["hash"] = identity_hash(data["mentions"])
    validated_map(IdentityMap(data))
    with pytest.raises(MigrationRefused, match="ordinal"):
        prepare_map_migration(
            case.store,
            case.request,
            case.registry,
            IdentityMap(data),
            selections=case.selections,
        )


def test_missing_creation_proof_never_adopts_matching_terms(migration_case):
    case = migration_case
    for key in list(case.wiki._recovery_pages):
        if key.endswith("/intent"):
            del case.wiki._recovery_pages[key]
    before = {
        key: item.get_json() for key, item in case.wiki.wbi.item.store.items()
    }
    with pytest.raises(MigrationRefused, match="binding"):
        case.prepare()
    assert {
        key: item.get_json() for key, item in case.wiki.wbi.item.store.items()
    } == before


def test_mutated_prepared_inventory_is_not_an_acceptance_token(
    migration_case, tmp_path
):
    case = migration_case
    prepared = case.prepare()
    approve(case, prepared)
    prepared.identity["summary"]["fictional_change"] = True
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(MigrationRefused, match="inputs changed"):
        accept_map_migration(
            case.store,
            prepared,
            case.registry,
            record_dir=tmp_path / "records",
        )
    assert case.wiki._recovery_pages == pages


def test_existing_ordinary_assignment_approval_needs_no_second_confirmation(
    migration_case, tmp_path
):
    case = migration_case
    prepared = case.prepare()
    approve(case, prepared)
    before = tuple(case.store.list_proposals())
    rebuilt = case.prepare()
    assert tuple(case.store.list_proposals()) == before
    receipt = accept_map_migration(
        case.store, rebuilt, case.registry, record_dir=tmp_path / "records"
    )
    assert len(receipt.assignments) == 2
    assert receipt.map_sha256 == record_digest(case.identity.data)


def test_selected_migration_does_not_accept_another_approved_source_mention(
    migration_case, tmp_path
):
    case = migration_case
    prepared = case.prepare()
    approve(case, prepared)
    other = prepare_map_migration(
        case.store,
        case.request,
        case.registry,
        case.identity,
        selections=(
            MentionKey(
                artifact_sha1=case.selections[0].artifact_sha1, local_id="c"
            ),
        ),
    )
    approve(case, other)
    selected = {c.mention_id for c in prepared.correspondences}
    unselected = other.correspondences[0].mention_id
    receipt = accept_map_migration(
        case.store, prepared, case.registry, record_dir=tmp_path / "records"
    )
    snapshot = case.registry.assignment_snapshot()
    assert set(snapshot.assignments) == selected
    assert unselected not in snapshot.assignments
    record = read_record(
        tmp_path / "records", RecordRef(sha256=receipt.record_sha256)
    )
    assert all(
        set(d["proposal"]["mentions"]) <= selected
        for d in record.metadata["identity_migration"]["decisions"].values()
    )


def test_harmless_local_id_change_keeps_assignment_and_original_confirmation(
    migration_case, tmp_path
):
    case = migration_case
    prepared = case.prepare()
    approve(case, prepared)
    original = accept_map_migration(
        case.store, prepared, case.registry, record_dir=tmp_path / "records"
    )
    source = case.request.sources[0]
    changed = source.batch.model_copy(deep=True)
    changed.entities[0].id = "renumbered"
    changed.entities.reverse()
    request = replace(case.request, sources=(replace(source, batch=changed),))
    current = prepare_map_migration(
        case.store,
        request,
        case.registry,
        case.identity,
        selections=(
            MentionKey(
                artifact_sha1=source.artifact_sha1, local_id="renumbered"
            ),
        ),
    )
    assert all(
        a.status == "applied"
        for a in current.review.projection.applications
        if a.decision
        and current.correspondences[0].mention_id
        in a.decision.proposal.mentions
    )
    kept = accept_map_migration(
        case.store, current, case.registry, record_dir=tmp_path / "records"
    )
    assignment = kept.assignments[0]
    assert assignment in original.assignments
    assert (
        kept.correspondences[0].mention_revision > assignment.mention_revision
    )
    assert len(case.registry.assignment_snapshot().history) == 1
