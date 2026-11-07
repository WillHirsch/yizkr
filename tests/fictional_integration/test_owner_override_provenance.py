"""Verified curator replacement is visible through the real graph query."""

from dataclasses import replace

import pytest
from wikibaseintegrator.datatypes import Item, String, Time
from wikibaseintegrator.models import Reference
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures import example_universe as u
from yizkr.claims import ArtifactRef, ClaimBatch, EntityRecord
from yizkr.core import sparql
from yizkr.core.provenance import RecordRef
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.identity_origins import owner_override_evidence
from yizkr.pipeline.identity_provenance import IdentityProvenanceRefused
from yizkr.pipeline.identity_review import ReviewStore
from yizkr.pipeline.owner_override_provenance import publish_owner_override
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.run_provenance import read_record
from yizkr.pipeline.transcribe import Transcription
from yizkr.queries.publish import load_templates, render_for_instance

from .test_assignment_writes import _entity
from .test_write_query import _synchronize

pytestmark = pytest.mark.live
WHEN = "2026-01-01T12:00:00Z"


def test_verified_owner_override_preserves_evidence_and_has_graph_provenance(
    fictional_client, tmp_path
):
    client = fictional_client
    properties, items = client.vocabulary()
    registry = CreationRegistry(client)
    original = u.by_key()["birth-extract-1938"]
    text = original.text + "\nFictional owner-override boundary fixture.\n"
    path = tmp_path / "owner-override-source.txt"
    path.write_text(text)
    artifact = ArtifactRef.from_path(path)
    source = register_artifact(
        client,
        path,
        "artifact",
        "Owner override: fictional birth extract",
        pages=1,
    )
    assert source.created
    bindings = []
    for key, name in (
        ("wrong", "Rudolf Löbig"),
        ("right", "Gustav Löbig"),
        ("ordinary", "Sebastian Löbig"),
    ):
        target = client.wbi.item.new()
        target.labels.set("en", f"Owner override: {name}")
        bindings.append(
            registry.create(
                {"kind": "fictional-owner-override", "target": key},
                target,
                summary="fixture: owner-override target",
            )
        )
    wrong, right, ordinary_target = bindings
    target = client.wbi.item.get(right.qid)
    target.labels.set("en", "Owner override: Gustav, curated label")
    source_ref = Reference()
    source_ref.add(
        Item(prop_nr=properties["stated_in"], value=source.artifact_qid)
    )
    source_ref.add(String(prop_nr=properties["page_number"], value="1"))
    kind = Item(prop_nr=properties["instance_of"], value=items["human"])
    kind.rank = WikibaseRank.PREFERRED
    kind.references.add(source_ref)
    birth = Time(
        prop_nr=properties["date_of_birth"],
        time="+1919-01-07T00:00:00Z",
        precision=11,
    )
    birth.references.add(source_ref)
    target.claims.add([kind, birth])
    write_existing(target, summary="fixture: source-backed human and birth")
    ordinary_item = client.wbi.item.get(ordinary_target.qid)
    ordinary_kind = Item(
        prop_nr=properties["instance_of"], value=items["human"]
    )
    ordinary_kind.references.add(source_ref)
    ordinary_item.claims.add(ordinary_kind)
    write_existing(
        ordinary_item, summary="fixture: ordinary source-backed kind"
    )
    before = _entity(client, right.qid)
    key = MentionKey(artifact_sha1=artifact.sha1, local_id="gustav")
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "authored-fictional-proposal",
                "pipeline_version": "fixture",
                "started_at": WHEN,
            },
            "entities": [
                EntityRecord(
                    id="gustav",
                    label="Gustav",
                    kind="human",
                    mention_lines=[1],
                ).model_dump(mode="json"),
                EntityRecord(
                    id="sebastian",
                    label="Sebastian Löbig",
                    kind="human",
                    mention_lines=[1],
                ).model_dump(mode="json"),
            ],
        }
    )
    old = ReviewProposal(
        operation="assign",
        mentions=(key,),
        target=wrong,
        origin={"method": "adjudicated", "run_id": "untyped-fictional-origin"},
    )
    ordinary_key = MentionKey(
        artifact_sha1=artifact.sha1, local_id="sebastian"
    )
    ordinary = ReviewProposal(
        operation="assign", mentions=(ordinary_key,), target=ordinary_target
    )
    request = ReviewRequest(
        sources=(
            ReviewSource(
                artifact.sha1,
                batch,
                Transcription(
                    artifact=artifact,
                    text=text,
                    language="de",
                    engine="fixture",
                ),
            ),
        ),
        proposals=(old, ordinary),
        current_targets=(wrong, right, ordinary_target),
    )
    with ReviewStore(tmp_path / "override.sqlite") as store:
        prepared = prepare_review(store, request)
        mention = prepared.mentions[key.key]
        ordinary_mention = prepared.mentions[ordinary_key.key]
        approved_ordinary = next(
            p
            for p in prepared.proposals
            if p.target.entity_id == ordinary_target.entity_id
        )
        store.decide(
            approved_ordinary.id,
            approved_ordinary.revision,
            expected_digest=approved_ordinary.digest,
            outcome="approve",
            actor="fictional curator",
            time=WHEN,
        )
        proposal = next(
            p
            for p in prepared.proposals
            if p.target.entity_id == wrong.entity_id
        )
        rejected = store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="reject",
            actor="fictional curator",
            time=WHEN,
        )
        evidence = owner_override_evidence(
            store,
            proposal.id,
            replacement_mention=mention,
            target=right,
            reason="The birth extract names Gustav, not the proposed Rudolf.",
        )
        replacement = ReviewProposal(
            operation="assign",
            mentions=(key,),
            target=right,
            evidence=evidence,
            origin={"method": "owner_override"},
        )
        current = replace(request, proposals=(old, replacement, ordinary))
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
            actor="fictional curator",
            time=WHEN,
        )
        accepted = accept_reviewed_assignments(store, current, registry)
    directory = tmp_path / "records"
    before_ordinary = _entity(client, ordinary_target.qid)
    with pytest.raises(
        IdentityProvenanceRefused, match="verified owner override"
    ):
        publish_owner_override(
            client, ordinary_mention.id, record_dir=directory
        )
    assert _entity(client, ordinary_target.qid) == before_ordinary
    assert not directory.exists()
    published = publish_owner_override(
        client, mention.id, record_dir=directory
    )
    assert published.changed and published.record_qid != right.qid
    after = _entity(client, right.qid)
    assert after["labels"] == before["labels"]
    assert (
        after["claims"][properties["date_of_birth"]]
        == before["claims"][properties["date_of_birth"]]
    )
    (old_kind,) = before["claims"][properties["instance_of"]]
    (new_kind,) = after["claims"][properties["instance_of"]]
    assert {k: v for k, v in old_kind.items() if k != "references"} == {
        k: v for k, v in new_kind.items() if k != "references"
    }
    old_hashes = {r["hash"] for r in old_kind["references"]}
    assert old_hashes <= {r["hash"] for r in new_kind["references"]}
    origins = [
        r
        for r in new_kind["references"]
        if properties["identity_method"] in r["snaks"]
    ]
    confirmations = [
        r
        for r in new_kind["references"]
        if properties["identity_confirmation_actor"] in r["snaks"]
    ]
    assert len(origins) == len(confirmations) == 1
    assert properties["identity_confirmation_actor"] not in origins[0]["snaks"]
    assert properties["identity_method"] not in confirmations[0]["snaks"]
    assert properties["identity_migration_id"] not in origins[0]["snaks"]
    digest = origins[0]["snaks"][properties["run_record_sha256"]][0][
        "datavalue"
    ]["value"]
    record = read_record(directory, RecordRef(sha256=digest))
    assert record.metadata["identity_owner_override"]["evidence"][
        "rejected"
    ] == rejected.model_dump(mode="json")
    assert record.metadata["identity_owner_override"][
        "confirmation"
    ] == confirmed.model_dump(mode="json")
    assert not publish_owner_override(
        client, mention.id, record_dir=directory
    ).changed
    assert _entity(client, right.qid) == after
    _synchronize(client)
    query = render_for_instance(
        load_templates()["identity-provenance"],
        properties,
        items,
        client.config.concept_uri,
    )
    uri = client.config.concept_uri + "/entity/"
    rows = [
        r
        for r in sparql.select(client.config.sparql_url, query)
        if r["record"]["value"] == uri + published.record_qid
    ]
    assert rows
    assert {r["entity"]["value"] for r in rows} == {uri + right.qid}
    assert {r["method"]["value"] for r in rows} == {
        uri + items["identity_owner_override"]
    }
    assert {r["assignmentRevision"]["value"] for r in rows} == {
        accepted.snapshot.assignments[mention.id].revision_id
    }
    assert {r["actor"]["value"] for r in rows} == {"fictional curator"}
    assert {r["confirmedAt"]["value"] for r in rows} == {WHEN}
    assert {r["recordFile"]["value"] for r in rows} == {digest + ".json"}
    assert not any(
        "migration" in r or "model" in r or "prompt" in r for r in rows
    )
    assert any(
        "unavailable" in r.get("limitation", {}).get("value", "") for r in rows
    )
