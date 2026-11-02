"""Verified identity origins preserve the existing graph evidence."""

from __future__ import annotations

import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

from tests.unit.test_review_stages import request
from tests.unit.test_run_statements import ITEMS, PROPERTIES, ProvenanceWiki
from yizkr.core.provenance import ProcessRecord, StageProvenance
from yizkr.core.write_recovery import (
    CreationRegistry,
    cluster_creation_operation,
)
from yizkr.pipeline.clustering import IdentityMap, Manifest, identity_hash
from yizkr.pipeline.identity_migration import (
    accept_map_migration,
    prepare_map_migration,
)
from yizkr.pipeline.identity_provenance import (
    METHOD_ITEMS,
    IdentityProvenanceRefused,
    publish_identity_provenance,
)
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.review_stages import MentionKey


class Wiki(ProvenanceWiki):
    def iter_entities(self, props):
        assert props == "claims"
        for qid, item in sorted(self.wbi.item.store.items()):
            yield qid, copy.deepcopy(item.get_json())


def fixture(tmp_path, method="singleton", *, producer=False):
    wiki = Wiki(detached=True)
    initial = replace(request(), proposals=())
    source = initial.sources[0]
    identity = IdentityMap.of([Manifest(source.artifact_sha1, source.batch)])
    row = next(
        r
        for r in identity.data["mentions"].values()
        if r["local_id"] == "leopold"
    )
    row["method"] = method
    identity.data["clusters"][row["cluster"]]["method"] = method
    identity.data["hash"] = identity_hash(identity.data["mentions"])
    if producer:
        # Authored metadata exercises preservation, without simulating a call.
        identity.data["provenance"] = ProcessRecord(
            kind="identity-map-production",
            metadata={"identity_map_hash": identity.hash},
            unratified=True,
            scratch_id="fictional-origin-run",
            stages=(
                StageProvenance(
                    stage="identity-adjudication",
                    process="model",
                    model_identifier="scripted:fictional-adjudicator",
                    prompt_hashes={"adjudication": "a" * 64},
                    limitations=("authored fictional producer metadata",),
                ),
            ),
        ).model_dump(mode="json")
    artifact = wiki.wbi.item.new()
    artifact.labels.set("en", "Fictional family names")
    artifact.claims.add(
        String(prop_nr=PROPERTIES["media_sha1"], value=source.artifact_sha1)
    )
    artifact.write()
    subject = wiki.wbi.item.new()
    subject.labels.set("en", "Leopold Löbig")
    statement = Item(prop_nr=PROPERTIES["instance_of"], value=ITEMS["human"])
    statement.rank = WikibaseRank.DEPRECATED
    source_ref = Reference()
    source_ref.add(Item(prop_nr=PROPERTIES["stated_in"], value=artifact.id))
    source_ref.add(String(prop_nr=PROPERTIES["page_number"], value="1"))
    statement.references.add(source_ref)
    subject.claims.add(statement)
    registry = CreationRegistry(wiki)
    binding = registry.create(
        cluster_creation_operation(identity.hash, row["cluster"]),
        subject,
        summary="fictional map-bound subject",
    )
    selection = MentionKey(
        artifact_sha1=source.artifact_sha1, local_id="leopold"
    )
    database = tmp_path / "review.sqlite"
    directory = tmp_path / "records"
    with ReviewStore(database) as store:
        prepared = prepare_map_migration(
            store, initial, registry, identity, selections=(selection,)
        )
        proposal = prepared.review.proposals[0]
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time="2026-01-01T12:00:00Z",
        )
        receipt = accept_map_migration(
            store, prepared, registry, record_dir=directory
        )
    return SimpleNamespace(
        wiki=wiki,
        registry=registry,
        receipt=receipt,
        directory=directory,
        qid=binding.qid,
        source_qid=artifact.id,
        initial=initial,
        identity=identity,
        selection=selection,
        database=database,
    )


def publish(f, **kwargs):
    return publish_identity_provenance(
        f.wiki, f.receipt.migration_id, record_dir=f.directory, **kwargs
    )


def kind(f):
    return f.wiki.wbi.item.get(f.qid).claims.get(PROPERTIES["instance_of"])[0]


def values(reference, key):
    return [
        s.datavalue["value"]
        for s in reference.snaks.get(PROPERTIES[key]) or []
    ]


@pytest.mark.parametrize(
    "method", sorted(set(METHOD_ITEMS) - {"owner_override"})
)
def test_original_method_and_confirmation_are_distinct_and_repeatable(
    tmp_path, method
):
    f = fixture(tmp_path, method)
    old = kind(f).get_json()
    result = publish(f)
    assert result.changed and result.record_qid != f.qid
    statement = kind(f).get_json()
    assert {k: v for k, v in statement.items() if k != "references"} == {
        k: v for k, v in old.items() if k != "references"
    }
    assert (
        statement["references"][: len(old["references"])] == old["references"]
    )
    refs = list(kind(f).references)
    origin = next(r for r in refs if values(r, "identity_method"))
    confirm = next(r for r in refs if values(r, "identity_confirmation_actor"))
    assert (
        values(origin, "identity_method")[0]["id"]
        == ITEMS[METHOD_ITEMS[method]]
    )
    assert not values(origin, "identity_confirmation_actor")
    assert not values(confirm, "identity_method")
    assert values(confirm, "identity_confirmation_actor") == ["curator"]
    assert values(origin, "identity_assignment_revision") == [
        f.receipt.assignments[0].revision_id
    ]
    assert values(origin, "run_record_sha256") == [f.receipt.record_sha256]
    record = f.wiki.wbi.item.get(result.record_qid)
    assert [
        s.mainsnak.datavalue["value"]["id"]
        for s in record.claims.get(PROPERTIES["instance_of"])
    ] == [ITEMS["identity_decision_record"]]
    assert any(
        "unavailable" in s.mainsnak.datavalue["value"]
        for s in record.claims.get(PROPERTIES["provenance_limitation"])
    )
    before = {
        qid: item.get_json() for qid, item in f.wiki.wbi.item.store.items()
    }
    assert not publish(f).changed
    assert before == {
        qid: item.get_json() for qid, item in f.wiki.wbi.item.store.items()
    }


def test_dry_run_is_read_only(tmp_path):
    f = fixture(tmp_path)
    items = copy.deepcopy(f.wiki.wbi.item.store)
    pages = copy.deepcopy(f.wiki._recovery_pages)
    result = publish(f, dry_run=True)
    assert result.record_qid is None and result.changed
    assert {k: v.get_json() for k, v in items.items()} == {
        k: v.get_json() for k, v in f.wiki.wbi.item.store.items()
    }
    assert f.wiki._recovery_pages == pages


@pytest.mark.parametrize(
    "damage",
    [
        "source",
        "duplicate-source",
        "kind",
        "duplicate-kind",
        "reference",
        "retired-source",
    ],
)
def test_missing_or_ambiguous_graph_support_refuses_before_effects(
    tmp_path, damage
):
    f = fixture(tmp_path)
    subject = f.wiki.wbi.item.get(f.qid)
    if damage == "source":
        del f.wiki.wbi.item.store[f.source_qid]
    elif damage == "retired-source":
        item = f.wiki.wbi.item.get(f.source_qid)
        item.claims.get(PROPERTIES["media_sha1"])[
            0
        ].rank = WikibaseRank.DEPRECATED
        item.write()
    elif damage == "duplicate-source":
        item = f.wiki.wbi.item.new()
        item.claims.add(
            String(
                prop_nr=PROPERTIES["media_sha1"],
                value=f.initial.sources[0].artifact_sha1,
            )
        )
        item.write()
    else:
        statement = subject.claims.get(PROPERTIES["instance_of"])[0]
        if damage == "kind":
            statement.mainsnak.datavalue["value"]["id"] = ITEMS["place"]
        elif damage == "reference":
            statement.references.references.clear()
        else:
            duplicate = copy.deepcopy(statement)
            duplicate.id = None
            subject.claims.add(
                duplicate, action_if_exists=ActionIfExists.FORCE_APPEND
            )
        subject.write()
    items = {k: v.get_json() for k, v in f.wiki.wbi.item.store.items()}
    pages = dict(f.wiki._recovery_pages)
    with pytest.raises(
        IdentityProvenanceRefused, match="missing or ambiguous"
    ):
        publish(f)
    assert items == {k: v.get_json() for k, v in f.wiki.wbi.item.store.items()}
    assert pages == f.wiki._recovery_pages


def test_corrupted_record_refuses_before_graph_effects(tmp_path):
    f = fixture(tmp_path)
    (f.directory / f"{f.receipt.record_sha256}.json").write_text("{}")
    writes = f.wiki.wbi.item.writes
    pages = dict(f.wiki._recovery_pages)
    with pytest.raises(ValueError):
        publish(f)
    assert f.wiki.wbi.item.writes == writes
    assert f.wiki._recovery_pages == pages


@pytest.mark.parametrize("after_target", [False, True])
def test_retry_recovers_partial_metadata_and_lost_write_response(
    tmp_path, monkeypatch, after_target
):
    import yizkr.pipeline.identity_provenance as module

    f = fixture(tmp_path)
    original = module.write_existing

    class Interrupted(BaseException):
        pass

    def interrupt(item, **kwargs):
        if after_target:
            original(item, **kwargs)
        raise Interrupted()

    with monkeypatch.context() as patch:
        patch.setattr(module, "write_existing", interrupt)
        with pytest.raises(Interrupted):
            publish(f)
    before = len(f.wiki.wbi.item.store)
    result = publish(f)
    assert len(f.wiki.wbi.item.store) == before
    assert result.changed is not after_target
    assert not publish(f).changed


def test_new_confirmation_preserves_original_assignment_origin(tmp_path):
    f = fixture(tmp_path)
    first = publish(f)
    original_assignment = f.receipt.assignments[0]
    original_refs = kind(f).references.get_json()
    with ReviewStore(f.database) as store:
        prepared = prepare_map_migration(
            store, f.initial, f.registry, f.identity, selections=(f.selection,)
        )
        proposal = prepared.review.proposals[0]
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="second curator",
            time="2026-01-02T12:00:00Z",
            expected_decision_revision=store.get_decision(
                proposal.id
            ).revision,
        )
        f.receipt = accept_map_migration(
            store, prepared, f.registry, record_dir=f.directory
        )
    assert f.receipt.assignments == (original_assignment,)
    second = publish(f)
    assert second.record_qid != first.record_qid
    assert kind(f).references.get_json()[: len(original_refs)] == original_refs
    assert any(
        values(r, "identity_confirmation_actor") == ["second curator"]
        for r in kind(f).references
    )


def test_known_adjudication_metadata_is_preserved_as_its_own_stage(tmp_path):
    f = fixture(tmp_path, "adjudicated", producer=True)
    result = publish(f)
    record = f.wiki.wbi.item.get(result.record_qid)
    stages = {
        s.mainsnak.datavalue["value"]: s
        for s in record.claims.get(PROPERTIES["run_stage"])
    }
    stage = stages["identity-adjudication"]
    assert [
        s.datavalue["value"]
        for s in stage.qualifiers.get(PROPERTIES["model_identifier"])
    ] == ["scripted:fictional-adjudicator"]
    assert [
        s.datavalue["value"]
        for s in stage.qualifiers.get(PROPERTIES["prompt_hash"])
    ] == ["adjudication:" + "a" * 64]
    assert not stages["identity-migration-preparation"].qualifiers.get(
        PROPERTIES["model_identifier"]
    )
    assert [
        s.mainsnak.datavalue["value"]
        for s in record.claims.get(PROPERTIES["scratch_run"]) or []
    ] == []
    origin = next(
        r for r in kind(f).references if values(r, "identity_method")
    )
    assert values(origin, "identity_origin_run") == ["fictional-origin-run"]
    (producer_sha,) = values(origin, "identity_origin_record_sha256")
    assert len(producer_sha) == 64 and producer_sha != f.receipt.record_sha256
    assert (f.directory / f"{producer_sha}.json").is_file()
    # The original scratch origin remains an unratified producer in the full
    # immutable record; migration has not asserted a whole-run ratification.


def test_retained_code_refusal_precedes_metadata_creation(
    tmp_path, monkeypatch
):
    import yizkr.pipeline.identity_provenance as module

    f = fixture(tmp_path)
    pages = dict(f.wiki._recovery_pages)
    writes = f.wiki.wbi.item.writes

    def refuse(client):
        raise ValueError("retained code refused")

    monkeypatch.setattr(module, "require_retained_code", refuse)
    with pytest.raises(ValueError, match="retained code refused"):
        publish(f)
    assert f.wiki._recovery_pages == pages
    assert f.wiki.wbi.item.writes == writes


@pytest.mark.parametrize("change_source", [False, True])
def test_publication_rechecks_graph_after_metadata_write(
    tmp_path, monkeypatch, change_source
):
    import yizkr.pipeline.identity_provenance as module

    f = fixture(tmp_path)
    original = module.ensure_run_statements

    def then_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        if change_source:
            item = f.wiki.wbi.item.get(f.source_qid)
            item.claims.get(PROPERTIES["media_sha1"])[
                0
            ].rank = WikibaseRank.DEPRECATED
        else:
            item = f.wiki.wbi.item.get(f.qid)
            item.claims.get(PROPERTIES["instance_of"])[
                0
            ].rank = WikibaseRank.PREFERRED
        item.write()
        return result

    monkeypatch.setattr(module, "ensure_run_statements", then_edit)
    if change_source:
        with pytest.raises(
            IdentityProvenanceRefused, match="missing or ambiguous"
        ):
            publish(f)
        assert not any(
            values(r, "identity_record") for r in kind(f).references
        )
    else:
        assert publish(f).changed
        assert kind(f).rank == WikibaseRank.PREFERRED
        assert any(values(r, "identity_record") for r in kind(f).references)
