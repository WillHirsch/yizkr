"""Protected cohort choices use real review, exclusion and creation receipts.

This case runs before other fixtures intentionally leave untyped legacy items.
Its readings are authored fiction; no provider call or simulated server guard
substitutes for the configured Wikibase persistence boundary.
"""

from __future__ import annotations

import json

import pytest
from wikibaseintegrator.datatypes import Item

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import ArtifactRef, GivenName, PersonStructure, Surname
from yizkr.core.mutation_guard import (
    CAPABILITY_HEADER,
    MutationGuardError,
    protected_mutation,
)
from yizkr.core.provenance import RecordRef
from yizkr.core.wiki import WikiClient
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline.assignment_writes import apply_reviewed_claims
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    ClaimSelection,
    prepare_claim_review,
    record_claim_decisions,
)
from yizkr.pipeline.cohort_additions import (
    apply_cohort_additions,
    prepare_cohort_additions,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.registrar import register_artifact
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
)
from yizkr.pipeline.run_provenance import read_record
from yizkr.pipeline.transcribe import Transcription

from .test_write_query import _synchronize

pytestmark = pytest.mark.live
WHEN = "2026-01-01T12:00:00Z"


class InterruptedCreation(BaseException):
    """Stop after a real item write but before its registry receipt."""


def _source(client, directory, key, suffix):
    case = source_case(key)
    path = directory / f"{suffix}.txt"
    path.write_bytes(
        case.source_bytes + f"\nProtected cohort {suffix}.\n".encode()
    )
    artifact = ArtifactRef.from_path(path)
    register_artifact(
        client,
        path,
        "certificate",
        f"Protected cohort {suffix}: {case.title}",
        pages=1,
    )
    batch = birth_batch(key)
    name = "Gustav" if key == "birth-extract-1938" else "Gustave LOBIG"
    batch.entities[0].label = name
    batch.entities[0].aliases = [name]
    batch.entities[0].mention_lines = [1]
    batch.entities[0].structure = PersonStructure(
        given=[GivenName(written=name.split()[0])],
        surnames=[Surname(written="LOBIG")] if " " in name else [],
    )
    batch.claims[0].source = batch.claims[0].source.model_copy(
        update={"artifact": artifact}
    )
    return ReviewSource(
        artifact.sha1,
        batch,
        Transcription(
            artifact=artifact,
            text=path.read_text(),
            language=case.language,
            engine="authored-fictional-text",
        ),
    )


def _seed(registry, name, key, aliases):
    properties, items = registry.client.vocabulary()
    item = registry.client.wbi.item.new()
    item.labels.set("en", name)
    item.descriptions.set("en", f"Protected cohort fixture {key}")
    item.aliases.set("en", aliases)
    item.claims.add(
        Item(prop_nr=properties["instance_of"], value=items["human"])
    )
    binding = registry.create(
        {"kind": "fictional-cohort-existing", "key": key},
        item,
        summary="fictional existing cohort candidate",
    )
    registry.index_binding(binding)
    return binding


def _request(source, operation, target=None):
    return ReviewRequest(
        sources=(source,),
        current_targets=(target,) if target else (),
        proposals=(
            ReviewProposal(
                operation=operation,
                mentions=(
                    MentionKey(
                        artifact_sha1=source.artifact_sha1, local_id="gustav"
                    ),
                ),
                target=target,
            ),
        ),
    )


def _approve(store, prepared):
    (proposal,) = prepared.review.proposals
    previous = store.get_decision(proposal.id)
    return store.decide(
        proposal.id,
        proposal.revision,
        expected_digest=proposal.digest,
        expected_decision_revision=previous.revision if previous else None,
        outcome="approve",
        actor="fictional cohort curator",
        time=WHEN,
    )


def _apply(store, request, registry, directory, operation):
    _synchronize(registry.client)
    return apply_cohort_additions(
        store,
        request,
        registry,
        state_path=directory / f"{operation}.capability",
        operation_id=operation,
        record_dir=directory / "records",
    )


def _claim(store, request, registry, directory):
    selections = (
        ClaimSelection(
            artifact_sha1=request.sources[0].artifact_sha1, index=0
        ),
    )
    prepared = prepare_claim_review(
        store, request, registry, selections=selections
    )
    assert all(claim.decision is None for claim in prepared.claims)
    record_claim_decisions(
        store,
        prepared,
        [
            ClaimDecisionInput(
                proposal_id=claim.proposal.id,
                expected_digest=claim.proposal.digest,
                outcome="approve",
                actor="fictional historical-claim curator",
                time=WHEN,
            )
            for claim in prepared.claims
        ],
    )
    fresh = prepare_claim_review(
        store, request, registry, selections=selections
    )
    written = apply_reviewed_claims(
        store, fresh, registry.client, record_dir=directory / "records"
    )
    assert len(written.receipts) == 1 and written.receipts[0].statement_guid
    repeated = apply_reviewed_claims(
        store,
        prepare_claim_review(store, request, registry, selections=selections),
        registry.client,
        record_dir=directory / "records",
    )
    assert repeated.receipts == written.receipts
    return written.receipts[0]


def _guard_rejects_competing_and_raw_writes(client, directory, target):
    competitor = WikiClient(client.config)
    session = competitor.login.get_session()
    token = competitor.login.get_edit_token()
    revision = client.entity_revisions([target])
    with protected_mutation(
        client,
        state_path=directory / "owner.capability",
        operation_id="cohort-owner",
    ) as guard:
        capability = client._mutation_capability
        assert capability is not None
        with (
            pytest.raises(MutationGuardError),
            protected_mutation(
                competitor,
                state_path=directory / "competitor.capability",
                operation_id="cohort-competitor",
            ),
        ):
            pytest.fail("competing operation acquired the active fence")
        response = session.post(
            competitor.api_url,
            data={
                "action": "wbeditentity",
                "id": target,
                "data": json.dumps(
                    {
                        "labels": {
                            "en": {
                                "language": "en",
                                "value": "Forbidden raw edit",
                            }
                        }
                    }
                ),
                "token": token,
                "format": "json",
            },
            timeout=30,
        )
        assert response.status_code == 423
        assert response.json()["error"]["code"] == "yizkr-mutation-excluded"
        guard.assert_active()
    assert client.entity_revisions([target]) == revision
    stale = session.post(
        competitor.api_url,
        headers={CAPABILITY_HEADER: capability},
        data={
            "action": "wbeditentity",
            "id": target,
            "data": json.dumps(
                {"labels": {"en": {"language": "en", "value": "Stale edit"}}}
            ),
            "token": token,
            "format": "json",
        },
        timeout=30,
    )
    assert stale.status_code == 423
    assert stale.json()["error"]["code"] == "yizkr-mutation-excluded"
    assert client.entity_revisions([target]) == revision
    released = session.post(
        competitor.api_url,
        data={
            "action": "wbeditentity",
            "id": target,
            "data": json.dumps(
                {
                    "descriptions": {
                        "en": {
                            "language": "en",
                            "value": "Outside writes resume after release",
                        }
                    }
                }
            ),
            "token": token,
            "format": "json",
        },
        timeout=30,
    )
    assert released.status_code == 200 and "error" not in released.json()
    assert client.entity_revisions([target])[target] > revision[target]


def test_protected_cohort_choices_claims_and_creation_recovery(
    fictional_client, tmp_path, monkeypatch
):
    client = WikiClient(fictional_client.config)
    registry = CreationRegistry(client)
    properties, _ = client.vocabulary()
    target = _seed(registry, "Gustav", "existing", ["Gustave LOBIG"])
    first = _source(client, tmp_path, "birth-extract-1938", "corroboration")
    second = _source(client, tmp_path, "naturalisation-1952", "distinct")
    _guard_rejects_competing_and_raw_writes(client, tmp_path, target.qid)
    _synchronize(client)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        corroborate = _request(first, "assign", target)
        prepared = prepare_cohort_additions(store, corroborate, registry)
        decision = _approve(store, prepared)
        applied = _apply(store, corroborate, registry, tmp_path, "corroborate")
        mention = prepared.review.mentions[first.artifact_sha1, "gustav"].id
        assert (
            applied.acceptance.snapshot.assignments[mention].entity_id
            == target.entity_id
        )
        assert not client.fetch_claims([target.qid])[target.qid].get(
            properties["date_of_birth"]
        )
        support = _claim(store, applied.request, registry, tmp_path)
        assert support.qid == target.qid
        (original_birth,) = client.fetch_claims([target.qid])[target.qid][
            properties["date_of_birth"]
        ]
        corroborating_source = _source(
            client, tmp_path, "birth-extract-1938", "corroborating-reading"
        )
        assert corroborating_source.artifact_sha1 != first.artifact_sha1
        corroborating_request = _request(
            corroborating_source, "assign", target
        )
        _synchronize(client)
        corroborating_review = prepare_cohort_additions(
            store, corroborating_request, registry
        )
        _approve(store, corroborating_review)
        corroborated = _apply(
            store,
            corroborating_request,
            registry,
            tmp_path,
            "corroborating-reading",
        )
        added_support = _claim(store, corroborated.request, registry, tmp_path)
        assert added_support.statement_guid == support.statement_guid
        assert added_support.primary_hash != support.primary_hash
        (supported_birth,) = client.fetch_claims([target.qid])[target.qid][
            properties["date_of_birth"]
        ]
        assert {
            key: value
            for key, value in supported_birth.items()
            if key != "references"
        } == {
            key: value
            for key, value in original_birth.items()
            if key != "references"
        }
        references = {
            reference["hash"]: reference
            for reference in supported_birth["references"]
        }
        assert all(
            references[reference["hash"]] == reference
            for reference in original_birth["references"]
        )
        assert support.primary_hash in references
        assert added_support.primary_hash in references
        assert store.get_decision(decision.proposal.id) == decision
        assert (
            registry.assignment_snapshot().assignments[mention]
            == applied.acceptance.snapshot.assignments[mention]
        )

        renamed = client.wbi.item.get(target.qid)
        renamed.labels.set("en", "Curator's existing cohort identity")
        write_existing(renamed, summary="fictional curator rename")
        _synchronize(client)
        carried = prepare_cohort_additions(store, corroborate, registry)
        assert carried.review.proposals[0].digest == decision.proposal.digest
        assert store.get_decision(decision.proposal.id) == decision
        repeated = _apply(
            store, corroborate, registry, tmp_path, "corroborate"
        )
        assert (
            repeated.acceptance.snapshot.assignments[mention]
            == applied.acceptance.snapshot.assignments[mention]
        )

        distinct = _request(second, "new_entity")
        _synchronize(client)
        proposed = prepare_cohort_additions(store, distinct, registry)
        original = _approve(store, proposed)
        old_revision = client.entity_revisions([target.qid])
        additional = _seed(registry, "Gustave LOBIG", "new-candidate", [])
        assert client.entity_revisions([target.qid]) == old_revision
        existing_items = set(client.list_entity_ids())
        assignments_before = registry.assignment_snapshot()
        authorizations_before = registry.reviewed_creation_authorizations()
        with pytest.raises(ReviewRefused):
            _apply(store, distinct, registry, tmp_path, "distinct")
        assert set(client.list_entity_ids()) == existing_items
        assert registry.assignment_snapshot() == assignments_before
        assert (
            registry.reviewed_creation_authorizations()
            == authorizations_before
        )
        refreshed = prepare_cohort_additions(store, distinct, registry)
        assert refreshed.review.proposals[0].digest != original.proposal.digest
        alternatives = refreshed.review.proposals[0].basis["evidence"][
            "cohort_candidates"
        ]["candidates"]
        assert {candidate["qid"] for candidate in alternatives} == {
            target.qid,
            additional.qid,
        }
        confirmed = _approve(store, refreshed)
        interrupted = []
        create = CreationRegistry.create

        def lose_reply(registry, operation, item, **kwargs):
            if (
                isinstance(operation, dict)
                and operation.get("kind") == "reviewed-entity-creation"
            ):
                write = item.write

                def lost(**options):
                    written = write(**options)
                    interrupted.append(written.id)
                    raise InterruptedCreation()

                item.write = lost
            return create(registry, operation, item, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(CreationRegistry, "create", lose_reply)
            with pytest.raises(InterruptedCreation):
                _apply(store, distinct, registry, tmp_path, "distinct")
        assert len(interrupted) == 1
        authorization = registry.reviewed_creation_authorization(confirmed)
        assert authorization is not None
        recovered = registry.reviewed_creation_binding(authorization)
        assert recovered is not None and recovered.qid == interrupted[0]
        current_assignments = registry.assignment_snapshot().assignments
        assert recovered.entity_id not in {
            assignment.entity_id for assignment in current_assignments.values()
        }
        item = client.wbi.item.get(recovered.qid)
        item.labels.set("en", "Curator's renamed kindless cohort identity")
        write_existing(
            item, summary="fictional rename after interrupted creation"
        )
        client = WikiClient(client.config)
        registry = CreationRegistry(client)
        created = _apply(store, distinct, registry, tmp_path, "distinct")
        assigned = refreshed.review.mentions[second.artifact_sha1, "gustav"].id
        assignment = created.acceptance.snapshot.assignments[assigned]
        assert assignment.entity_id == recovered.entity_id
        assert assignment.decision_references[0].id == confirmed.id
        assert (
            registry.reviewed_creation_authorization(confirmed)
            == authorization
        )
        assert (
            registry.binding(assignment.entity_id).binding.qid == recovered.qid
        )
        assert (
            client.wbi.item.get(recovered.qid).labels.get("en").value
            == "Curator's renamed kindless cohort identity"
        )
        claims = client.fetch_claims([recovered.qid])[recovered.qid]
        assert not claims.get(properties["instance_of"])
        (link,) = claims[properties["identity_record"]]
        digests = {
            snak["datavalue"]["value"]
            for reference in link["references"]
            for snak in reference["snaks"].get(
                properties["run_record_sha256"], []
            )
        }
        (digest,) = digests
        record = read_record(tmp_path / "records", RecordRef(sha256=digest))
        proof = record.metadata["identity_cohort"]
        assert proof["confirmation"] == confirmed.model_dump(mode="json")
        assert proof["confirmation"]["proposal"]["operation"] == "new_entity"
        assert proof["creation_authorization"] == authorization.model_dump(
            mode="json"
        )

        third = _source(
            client, tmp_path, "naturalisation-1952", "later-kindless-match"
        )
        _synchronize(client)
        later = prepare_cohort_additions(
            store, _request(third, "assign", recovered), registry
        )
        (operational,) = [
            candidate
            for candidate in later.candidates.mentions[0].candidates
            if candidate.qid == recovered.qid
        ]
        assert operational.graph is None and operational.creations
        assert (
            operational.creations[0].authorization.context.label
            == "Gustave LOBIG"
        )
        created_support = _claim(store, created.request, registry, tmp_path)
        assert created_support.qid == recovered.qid
        assert created_support.statement_guid != support.statement_guid
        assert store.get_decision(confirmed.proposal.id) == confirmed
