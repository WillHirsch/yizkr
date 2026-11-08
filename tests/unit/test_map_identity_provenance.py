"""Unratified map origins retain exact producer evidence without acceptance."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference

from tests.unit.test_clustering import day, identity_of, judgment, tree_union
from tests.unit.test_identity_provenance import Wiki, kind, values
from tests.unit.test_review_stages import request
from tests.unit.test_run_statements import ITEMS, PROPERTIES
from yizkr.core.identity_migration import MigrationRefused
from yizkr.core.provenance import ProcessRecord, RecordRef, StageProvenance
from yizkr.core.write_recovery import (
    CreationRegistry,
    cluster_creation_operation,
)
from yizkr.pipeline.clustering import IdentityMap, Manifest, identity_hash
from yizkr.pipeline.identity_migration import validated_map
from yizkr.pipeline.identity_provenance import (
    METHOD_ITEMS,
    IdentityProvenanceRefused,
)
from yizkr.pipeline.map_identity_provenance import (
    MapOriginSelection,
    prepare_map_origin,
    publish_map_origin,
    validate_map_origin,
)
from yizkr.pipeline.run_provenance import read_record, record_digest


def fixture(tmp_path, method="singleton", *, producer=True):
    wiki = Wiki(detached=True)
    source = request().sources[0]
    identity = IdentityMap.of([Manifest(source.artifact_sha1, source.batch)])
    row = next(
        row
        for row in identity.data["mentions"].values()
        if row["local_id"] == "leopold"
    )
    row["method"] = method
    identity.data["clusters"][row["cluster"]]["method"] = method
    identity.data["hash"] = identity_hash(identity.data["mentions"])
    if producer:
        identity.data["provenance"] = ProcessRecord(
            kind="identity-map-production",
            metadata={
                "identity_map_hash": identity.hash,
                "producer_run_id": "fictional-map-run",
            },
            stages=(
                StageProvenance(
                    stage="identity-adjudication",
                    process="model",
                    model_identifier="scripted:fictional-adjudicator",
                    prompt_hashes={"adjudication": "a" * 64},
                ),
            ),
        ).model_dump(mode="json")
    artifact = wiki.wbi.item.new()
    artifact.claims.add(
        String(prop_nr=PROPERTIES["media_sha1"], value=source.artifact_sha1)
    )
    artifact.write()
    subject = wiki.wbi.item.new()
    statement = Item(prop_nr=PROPERTIES["instance_of"], value=ITEMS["human"])
    reference = Reference()
    reference.add(Item(prop_nr=PROPERTIES["stated_in"], value=artifact.id))
    statement.references.add(reference)
    subject.claims.add(statement)
    binding = CreationRegistry(wiki).create(
        cluster_creation_operation(identity.hash, row["cluster"]),
        subject,
        summary="fictional map creation",
    )
    return SimpleNamespace(
        wiki=wiki,
        qid=binding.qid,
        source_qid=artifact.id,
        identity=identity,
        selection=MapOriginSelection(
            document=source.artifact_sha1,
            cluster_id=row["cluster"],
            artifact_sha1=source.artifact_sha1,
            kind="human",
            binding=binding,
        ),
        directory=tmp_path / "records",
    )


def publish(f, **kwargs):
    return publish_map_origin(
        f.wiki,
        prepare_map_origin(f.identity),
        selections=(f.selection,),
        record_dir=f.directory,
        **kwargs,
    )


def snapshot(f):
    return (
        {key: item.get_json() for key, item in f.wiki.wbi.item.store.items()},
        dict(f.wiki._recovery_pages),
    )


@pytest.mark.parametrize(
    "method", sorted(set(METHOD_ITEMS) - {"owner_override"})
)
def test_map_origins_never_imply_confirmation_and_repeat_exactly(
    tmp_path, method
):
    f = fixture(tmp_path, method)
    old = kind(f).get_json()
    result = publish(f)
    statement = kind(f).get_json()
    assert {k: v for k, v in statement.items() if k != "references"} == {
        k: v for k, v in old.items() if k != "references"
    }
    assert (
        statement["references"][: len(old["references"])] == old["references"]
    )
    origin = next(
        r for r in kind(f).references if values(r, "identity_method")
    )
    assert (
        values(origin, "identity_method")[0]["id"]
        == ITEMS[METHOD_ITEMS[method]]
    )
    assert values(origin, "identity_cluster") == [f.selection.cluster_id]
    assert values(origin, "identity_origin_run") == ["fictional-map-run"]
    for key in (
        "identity_assignment_revision",
        "identity_decision_revision",
        "identity_confirmation_actor",
        "identity_confirmation_at",
    ):
        assert not values(origin, key)
    record = read_record(
        f.directory,
        RecordRef(sha256=values(origin, "run_record_sha256")[0]),
    )
    producer = ProcessRecord.model_validate(f.identity.data["provenance"])
    assert record.metadata["identity_map"] == f.identity.data
    assert record.unratified is True
    assert record.stages == producer.stages
    assert record.producer_refs == (RecordRef(sha256=record_digest(producer)),)
    assert read_record(f.directory, record.producer_refs[0]) == producer
    before = snapshot(f)
    assert not publish(f).changed
    assert snapshot(f) == before
    assert result.record_qid != f.qid


def test_preparation_owns_snapshot_and_never_invents_legacy_metadata(tmp_path):
    f = fixture(tmp_path, producer=False)
    prepared = prepare_map_origin(f.identity)
    original = prepared.record
    original.metadata["identity_map"]["hash"] = "b" * 64
    f.identity.data["mentions"].clear()
    assert prepared.record.metadata["identity_map"]["mentions"]
    assert prepared.record.metadata["identity_map_hash"] == prepared.map_hash
    assert prepared.record.code is None
    assert prepared.record.manifest_hash is None
    assert prepared.record.scratch_id is None
    assert prepared.record.stages == ()
    assert prepared.record.producer_refs == ()
    assert "unavailable" in " ".join(prepared.record.limitations)
    assert not f.directory.exists()


def test_dry_run_leaves_graph_authority_and_disk_unchanged(tmp_path):
    f = fixture(tmp_path)
    before = snapshot(f)
    result = publish(f, dry_run=True)
    assert result.record_qid is None and result.changed
    assert snapshot(f) == before
    assert not f.directory.exists()


@pytest.mark.parametrize("damage", ["method", "producer", "map"])
def test_preparation_rejects_invalid_full_map_before_effects(tmp_path, damage):
    f = fixture(tmp_path)
    if damage == "method":
        # A different, unselected cluster must still pass full-map validation.
        cluster = next(
            row
            for cid, row in f.identity.data["clusters"].items()
            if cid != f.selection.cluster_id
        )
        cluster["method"] = "owner_override"
    elif damage == "producer":
        f.identity.data["provenance"]["metadata"]["identity_map_hash"] = (
            "b" * 64
        )
    else:
        f.identity.data["hash"] = "b" * 64
    before = snapshot(f)
    with pytest.raises(IdentityProvenanceRefused):
        publish(f)
    assert snapshot(f) == before
    assert not f.directory.exists()


@pytest.mark.parametrize("damage", ["document", "binding", "source"])
def test_publication_rejects_unverified_placement_or_source(tmp_path, damage):
    f = fixture(tmp_path)
    if damage == "document":
        f.selection = replace(f.selection, document="f" * 40)
    elif damage == "binding":
        f.selection = replace(
            f.selection,
            binding=f.selection.binding.model_copy(update={"qid": "Q999"}),
        )
    else:
        duplicate = f.wiki.wbi.item.new()
        duplicate.claims.add(
            String(
                prop_nr=PROPERTIES["media_sha1"],
                value=f.selection.artifact_sha1,
            )
        )
        duplicate.write()
    before = snapshot(f)
    with pytest.raises(IdentityProvenanceRefused):
        publish(f)
    assert snapshot(f) == before


def test_preflight_checks_methods_in_unselected_clusters(tmp_path):
    f = fixture(tmp_path, "adjudicated")
    prepared = prepare_map_origin(f.identity)
    properties, items = f.wiki.vocabulary()
    del items[METHOD_ITEMS["singleton"]]
    f.wiki.vocabulary = lambda: (properties, items)
    before = snapshot(f)
    with pytest.raises(IdentityProvenanceRefused, match="bootstrap"):
        validate_map_origin(f.wiki, prepared)
    assert snapshot(f) == before


def test_authentic_mixed_event_kinds_remain_valid_for_ordinary_origins():
    data = identity_of(judgment(), tree_union(day(1947), "wedding"))
    identity = IdentityMap(data)
    assert any(
        len({data["mentions"][m]["kind"] for m in c["members"]}) > 1
        for c in data["clusters"].values()
    )
    prepared = prepare_map_origin(identity)
    assert prepared.record.metadata["identity_map"] == data
    with pytest.raises(MigrationRefused, match="kind"):
        validated_map(identity)


def test_same_decisions_with_another_producer_keep_both_origins(tmp_path):
    f = fixture(tmp_path)
    first = publish(f)
    map_hash = f.identity.hash
    f.identity.data["provenance"]["metadata"]["producer_run_id"] = (
        "fictional-later-run"
    )
    second = publish(f)
    assert f.identity.hash == map_hash
    assert first.record_qid != second.record_qid
    assert f.wiki.wbi.item.get(first.record_qid).labels.get("en").value != (
        f.wiki.wbi.item.get(second.record_qid).labels.get("en").value
    )
    origins = [r for r in kind(f).references if values(r, "identity_method")]
    assert {values(r, "identity_origin_run")[0] for r in origins} == {
        "fictional-map-run",
        "fictional-later-run",
    }
    assert not publish(f).changed


def test_actual_source_and_reviewed_kind_can_differ_from_the_map(tmp_path):
    f = fixture(tmp_path)
    source = f.wiki.wbi.item.new()
    source.claims.add(String(prop_nr=PROPERTIES["media_sha1"], value="e" * 40))
    source.write()
    subject = f.wiki.wbi.item.get(f.qid)
    statement = Item(prop_nr=PROPERTIES["instance_of"], value=ITEMS["place"])
    reference = Reference()
    reference.add(Item(prop_nr=PROPERTIES["stated_in"], value=source.id))
    statement.references.add(reference)
    subject.claims.add(statement)
    subject.write()
    f.selection = replace(f.selection, artifact_sha1="e" * 40, kind="place")
    assert publish(f).changed
    kinds = f.wiki.wbi.item.get(f.qid).claims.get(PROPERTIES["instance_of"])
    original, reviewed = kinds
    assert not any(values(r, "identity_record") for r in original.references)
    assert any(values(r, "identity_record") for r in reviewed.references)


def test_duplicate_source_during_target_write_prevents_success(
    tmp_path, monkeypatch
):
    import yizkr.pipeline.identity_provenance as module

    f = fixture(tmp_path)
    original = module.write_existing

    def then_duplicate(item, **kwargs):
        result = original(item, **kwargs)
        if item.id == f.qid:
            duplicate = f.wiki.wbi.item.new()
            duplicate.claims.add(
                String(
                    prop_nr=PROPERTIES["media_sha1"],
                    value=f.selection.artifact_sha1,
                )
            )
            duplicate.write()
        return result

    monkeypatch.setattr(module, "write_existing", then_duplicate)
    with pytest.raises(IdentityProvenanceRefused, match="ambiguous"):
        publish(f)
    # Publication is additive, not a transaction. An incomplete attempt must
    # refuse success even when the conflicting source appeared after a write.
    assert any(values(r, "identity_record") for r in kind(f).references)


def test_publication_reads_scale_linearly_with_target_count(
    tmp_path, monkeypatch
):
    f = fixture(tmp_path)
    selections = [f.selection]
    registry = CreationRegistry(f.wiki)
    for cid, cluster in f.identity.data["clusters"].items():
        if cid == f.selection.cluster_id:
            continue
        item = f.wiki.wbi.item.new()
        claim = Item(
            prop_nr=PROPERTIES["instance_of"], value=ITEMS[cluster["kind"]]
        )
        reference = Reference()
        reference.add(
            Item(prop_nr=PROPERTIES["stated_in"], value=f.source_qid)
        )
        claim.references.add(reference)
        item.claims.add(claim)
        binding = registry.create(
            cluster_creation_operation(f.identity.hash, cid),
            item,
            summary="another fictional map creation",
        )
        selections.append(
            replace(
                f.selection,
                cluster_id=cid,
                kind=cluster["kind"],
                binding=binding,
            )
        )
    assert len(selections) >= 2
    scans = []
    lookups = []
    original = f.wiki.iter_entities
    original_lookup = CreationRegistry.lookup

    def counted(props):
        scans.append(props)
        return original(props)

    f.wiki.iter_entities = counted

    def counted_lookup(registry, operation):
        if isinstance(operation, dict) and operation.get("kind") == "cluster":
            lookups.append(operation)
        return original_lookup(registry, operation)

    monkeypatch.setattr(CreationRegistry, "lookup", counted_lookup)
    result = publish_map_origin(
        f.wiki,
        prepare_map_origin(f.identity),
        selections=selections,
        record_dir=f.directory,
    )
    assert {qid for qid, _ in result.statements} == {
        selection.qid for selection in selections
    }
    assert len(scans) <= 3
    assert len(lookups) <= 4 * len(selections)
