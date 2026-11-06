"""Explicit replacement provenance preserves graph evidence and human edits."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import Item, String
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import ActionIfExists, WikibaseRank

from tests.unit.test_assignment_stages import WHEN, binding
from tests.unit.test_identity_provenance import Wiki, values
from tests.unit.test_review_stages import request
from tests.unit.test_run_statements import ITEMS, PROPERTIES
from yizkr.core.provenance import ProcessRecord, RecordRef, StageProvenance
from yizkr.core.write_recovery import CreationRegistry
from yizkr.pipeline import identity_provenance
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.identity_origins import owner_override_evidence
from yizkr.pipeline.identity_provenance import IdentityProvenanceRefused
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.owner_override_provenance import publish_owner_override
from yizkr.pipeline.review_stages import ReviewProposal, prepare_review
from yizkr.pipeline.run_provenance import read_record, record_digest


def fixture(tmp_path, *, producer=False, ordinary=False):
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    wrong, right = binding(wiki, 1), binding(wiki, 2)
    initial = request()
    key = initial.proposals[0].mentions[0]
    origin = {"method": "adjudicated", "run_id": "untyped-fictional-origin"}
    original = None
    if producer:
        original = ProcessRecord(
            kind="identity-map-production",
            metadata={"producer_run_id": "fictional-verified-origin"},
            stages=(
                StageProvenance(
                    stage="identity-adjudication",
                    process="model",
                    model_identifier="scripted:fictional-adjudicator",
                    prompt_hashes={"adjudication": "b" * 64},
                ),
            ),
        )
        origin["provenance"] = original.model_dump(mode="json")
    old = ReviewProposal(
        operation="assign", mentions=(key,), target=wrong, origin=origin
    )
    initial = replace(
        initial, proposals=(old,), current_targets=(wrong, right)
    )
    with ReviewStore(tmp_path / "override.sqlite") as store:
        prepared = prepare_review(store, initial)
        mention = prepared.mentions[key.key]
        prior = prepared.proposals[0]
        rejected = store.decide(
            prior.id,
            prior.revision,
            expected_digest=prior.digest,
            outcome="reject",
            actor="curator",
            time=WHEN,
        )
        evidence = owner_override_evidence(
            store,
            prior.id,
            replacement_mention=mention,
            target=right,
            reason="The rejected candidate names the other occurrence.",
        )
        replacement = ReviewProposal(
            operation="assign",
            mentions=(key,),
            target=right,
            evidence={} if ordinary else evidence,
            origin={} if ordinary else {"method": "owner_override"},
        )
        current = replace(initial, proposals=(old, replacement))
        prepared = prepare_review(store, current)
        proposal = next(
            p
            for p in prepared.proposals
            if p.target.entity_id == right.entity_id
        )
        confirmed = store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
        )
        accepted = accept_reviewed_assignments(store, current, registry)
    artifact = wiki.wbi.item.new()
    artifact.labels.set("en", "Fictional family names")
    artifact.claims.add(
        String(prop_nr=PROPERTIES["media_sha1"], value=key.artifact_sha1)
    )
    artifact.write()
    target = wiki.wbi.item.get(right.qid)
    target.labels.set("en", "Leopold Löbig, curator's label")
    statement = Item(prop_nr=PROPERTIES["instance_of"], value=ITEMS["human"])
    statement.rank = WikibaseRank.PREFERRED
    source = Reference()
    source.add(Item(prop_nr=PROPERTIES["stated_in"], value=artifact.id))
    source.add(String(prop_nr=PROPERTIES["page_number"], value="1"))
    statement.references.add(source)
    target.claims.add(statement)
    target.write()
    return SimpleNamespace(
        wiki=wiki,
        registry=registry,
        target=right.qid,
        mention=mention.id,
        rejected=rejected,
        confirmed=confirmed,
        accepted=accepted,
        original=original,
        directory=tmp_path / "records",
    )


def publish(case):
    return publish_owner_override(
        case.wiki, case.mention, record_dir=case.directory
    )


def kind(case):
    return case.wiki.wbi.item.get(case.target).claims.get(
        PROPERTIES["instance_of"]
    )[0]


@pytest.mark.parametrize("producer", [False, True])
def test_verified_override_has_separate_human_confirmation_and_no_fake_origin(
    tmp_path, producer
):
    case = fixture(tmp_path, producer=producer)
    before = case.wiki.wbi.item.get(case.target).get_json()
    result = publish(case)
    assert result.changed and result.record_qid != case.target
    after = case.wiki.wbi.item.get(case.target).get_json()
    assert after["labels"] == before["labels"]
    old_statement = before["claims"][PROPERTIES["instance_of"]][0]
    statement = after["claims"][PROPERTIES["instance_of"]][0]
    assert {k: v for k, v in statement.items() if k != "references"} == {
        k: v for k, v in old_statement.items() if k != "references"
    }
    assert statement["references"][:1] == old_statement["references"]
    origin = next(
        r for r in kind(case).references if values(r, "identity_method")
    )
    confirm = next(
        r
        for r in kind(case).references
        if values(r, "identity_confirmation_actor")
    )
    assert values(origin, "identity_method") == [
        {
            "entity-type": "item",
            "numeric-id": int(ITEMS["identity_owner_override"][1:]),
            "id": ITEMS["identity_owner_override"],
        }
    ]
    assert not values(origin, "identity_migration_id")
    assert not values(origin, "identity_confirmation_actor")
    assert not values(confirm, "identity_method")
    assert values(confirm, "identity_confirmation_actor") == ["curator"]
    (digest,) = values(origin, "run_record_sha256")
    record = read_record(case.directory, RecordRef(sha256=digest))
    retained = record.metadata["identity_owner_override"]
    assert retained["evidence"]["rejected"] == case.rejected.model_dump(
        mode="json"
    )
    assert retained["confirmation"] == case.confirmed.model_dump(mode="json")
    if producer:
        assert values(origin, "identity_origin_record_sha256") == [
            record_digest(case.original)
        ]
        assert values(origin, "identity_origin_run") == [
            "fictional-verified-origin"
        ]
        assert record.stages[0] == case.original.stages[0]
    else:
        assert not values(origin, "identity_origin_record_sha256")
        assert not values(origin, "identity_origin_run")
        assert any(
            "unavailable" in limitation for limitation in record.limitations
        )
        assert all(
            s.model_identifier is None and not s.prompt_hashes
            for s in record.stages
        )
    assert record.stages[-1].process == "manual"
    writes = case.wiki.wbi.item.writes
    assert not publish(case).changed
    assert case.wiki.wbi.item.writes == writes


def test_ordinary_assignment_cannot_be_published_as_override(tmp_path):
    case = fixture(tmp_path, ordinary=True)
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(
        IdentityProvenanceRefused, match="verified owner override"
    ):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages
    assert not case.directory.exists()


@pytest.mark.parametrize("damage", ["missing", "ambiguous"])
def test_missing_or_ambiguous_kind_is_refused_before_graph_effects(
    tmp_path, damage
):
    case = fixture(tmp_path)
    target = case.wiki.wbi.item.get(case.target)
    if damage == "missing":
        target.claims.get(PROPERTIES["instance_of"])[
            0
        ].references.references.clear()
    else:
        statement = Item(
            prop_nr=PROPERTIES["instance_of"], value=ITEMS["human"]
        )
        statement.references = kind(case).references
        target.claims.add(
            statement, action_if_exists=ActionIfExists.FORCE_APPEND
        )
    target.write()
    writes = case.wiki.wbi.item.writes
    pages = dict(case.wiki._recovery_pages)
    with pytest.raises(
        IdentityProvenanceRefused, match="missing or ambiguous"
    ):
        publish(case)
    assert case.wiki.wbi.item.writes == writes
    assert case.wiki._recovery_pages == pages


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_projection_recovers_metadata_and_exact_refs(
    tmp_path, monkeypatch, committed
):
    case = fixture(tmp_path)
    original = identity_provenance.write_existing

    def interrupt(entity, **kwargs):
        if committed:
            original(entity, **kwargs)
        raise OSError("fictional lost projection response")

    with monkeypatch.context() as patch:
        patch.setattr(identity_provenance, "write_existing", interrupt)
        with pytest.raises(OSError, match="lost projection"):
            publish(case)
    record_items = set(case.wiki.wbi.item.store)
    result = publish(case)
    assert result.record_qid in record_items
    assert result.changed is not committed
    assert set(case.wiki.wbi.item.store) == record_items
    assert (
        len([r for r in kind(case).references if values(r, "identity_method")])
        == 1
    )
