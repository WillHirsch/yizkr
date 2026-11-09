"""Protected cohort choices use real review, exclusion and creation receipts.

This case runs before other fixtures intentionally leave untyped legacy items.
Its readings are authored fiction; no provider call or simulated server guard
substitutes for the configured Wikibase persistence boundary.
"""

from __future__ import annotations

import json

import pytest
import requests
from wikibaseintegrator import wbi_helpers
from wikibaseintegrator.datatypes import Item
from wikibaseintegrator.wbi_enums import WikibaseRank

from tests.fixtures.semantic_acceptance import birth_batch, source_case
from yizkr.claims import ArtifactRef, GivenName, PersonStructure, Surname
from yizkr.core.instance import (
    KEPT_STATE_PAGE,
    carries_kept_marker,
    kept_path,
    mark_kept,
    watermark,
)
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
from yizkr.pipeline.text_ingestion import TextIngestion
from yizkr.pipeline.text_products import TextSettings
from yizkr.pipeline.transcribe import TranscribedPage, Transcription

from .test_write_query import _synchronize

pytestmark = pytest.mark.live
WHEN = "2026-01-01T12:00:00Z"


class InterruptedCreation(BaseException):
    """Stop after a real item write but before its registry receipt."""


class TextAnswers:
    """Fixed fictional answers exercise real parsers, not model quality."""

    model_identifier = "authored-fictional-retained-text"

    def __init__(self, *answers):
        self.answers = iter(answers)
        self.calls = 0

    def complete(self, prompt, image_paths=()):
        self.calls += 1
        return next(self.answers)

    def reject(self, response, reason):
        return False


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
            pages=[
                TranscribedPage(
                    page=1,
                    start=0,
                    end=len(path.read_text()),
                    status="transcribed",
                    provenance={
                        "method": "text-layer",
                        "engine": "authored-fictional-text",
                    },
                )
            ],
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


def _retained_text_source(client, directory, target, old_support, monkeypatch):
    """A retained source uses saved products and recovers a lost receipt."""
    from yizkr.pipeline import assigned_support
    from yizkr.pipeline.assignment_writes import ApplicationInterrupted

    case = source_case("birth-extract-1938")
    source = directory / "retained-birth-extract.txt"
    source.write_text(case.text + "\nRetained fictional certificate copy.\n")
    artifact = ArtifactRef.from_path(source)
    exclusions = directory / "fictional-exclusions.txt"
    exclusions.write_text("# Explicitly empty for repository fiction.\n")
    batch = birth_batch(case.key)
    entity = batch.entities[0]
    entity.label = "Gustav"
    entity.aliases = ["Gustav"]
    entity.mention_lines = [1]
    entity.structure = PersonStructure(given=[GivenName(written="Gustav")])
    claim = batch.claims[0].model_copy(
        update={
            "source": batch.claims[0].source.model_copy(
                update={"artifact": artifact, "page_number": None}
            )
        }
    )
    extractor = TextAnswers(
        json.dumps(
            {
                "entities": [entity.model_dump(mode="json")],
                "preparation": None,
            }
        ),
        claim.model_dump_json() + '\n{"done":true}',
    )
    critic = TextAnswers('{"done":true}')
    properties, _ = client.vocabulary()
    item = client.wbi.item.get(target.qid)
    (birth,) = item.claims.get(properties["date_of_birth"])
    birth.rank = WikibaseRank.PREFERRED
    item.labels.set("en", "Curator's corrected Gustav identity")
    item.descriptions.set("en", "Curator's retained certificate annotation")
    write_existing(item, summary="fictional curator corrections to retain")
    before = item.get_json()
    (old_birth,) = before["claims"][properties["date_of_birth"]]
    database = directory / "retained-review.sqlite"
    products_dir = directory / "retained-text"
    marker = kept_path(client.config)
    assert not marker.exists() and not carries_kept_marker(client)
    original_write = assigned_support.write_existing
    written = []
    attempted_receipts = []

    def counted_write(item, **kwargs):
        result = original_write(item, **kwargs)
        written.append(item.id)
        return result

    def lose_receipt(registry, receipt):
        attempted_receipts.append(receipt)
        raise OSError("fictional interruption after retained support write")

    try:
        mark_kept(client, "fictional retained-ingestion curator")
        assert marker.exists() and carries_kept_marker(client)
        with ReviewStore(database) as store:
            workflow = TextIngestion(
                source,
                products_dir,
                client=client,
                review_store=store,
                exclusions_file=exclusions,
            )
            products = workflow.prepare(
                settings=TextSettings(strategy="chunked", language="de"),
                extractor_client=extractor,
                critic_client=critic,
            )
            assert (extractor.calls, critic.calls) == (2, 1)
            assert tuple(
                stage.stage for stage in products.critique.provenance.stages
            ) == ("transcription", "extraction", "critique")
            registration = workflow.prepare_registration(
                "certificate", "Retained fictional birth certificate"
            )
            workflow.register_source(
                registration,
                expected_digest=registration.digest,
                actor="fictional source curator",
            )
            _synchronize(client)
            prepared = workflow.prepare_identities(
                (
                    ReviewProposal(
                        operation="assign",
                        mentions=(
                            MentionKey(
                                artifact_sha1=artifact.sha1,
                                local_id="gustav",
                            ),
                        ),
                        target=target,
                    ),
                )
            )
            (review_source,) = prepared.request.sources
            assert review_source.reading.text == source.read_text()
            assert review_source.reading.artifact == artifact
            assert review_source.batch.claims[0].source.page_number is None
            identity_decision = _approve(store, prepared)
            accepted = workflow.apply_identities(("gustav",))
            mention = prepared.review.mentions[artifact.sha1, "gustav"].id
            assignment = accepted.acceptance.snapshot.assignments[mention]
            assert assignment.entity_id == target.entity_id
            historical = workflow.prepare_claims((0,))
            (reviewed_claim,) = historical.claims
            assert reviewed_claim.decision is None
            record_claim_decisions(
                store,
                historical,
                [
                    ClaimDecisionInput(
                        proposal_id=reviewed_claim.proposal.id,
                        expected_digest=reviewed_claim.proposal.digest,
                        outcome="approve",
                        actor="fictional retained-claim curator",
                        time=WHEN,
                    )
                ],
            )
            with monkeypatch.context() as patch:
                patch.setattr(
                    assigned_support, "write_existing", counted_write
                )
                patch.setattr(
                    CreationRegistry,
                    "record_application_receipt",
                    lose_receipt,
                )
                with pytest.raises(ApplicationInterrupted) as stopped:
                    workflow.apply_claims((0,))
            assert len(written) == len(attempted_receipts) == 1
            assert stopped.value.receipts == ()

        # Resume with fresh graph/review clients and no model-response cache.
        client = WikiClient(client.config)
        with ReviewStore(database) as store:
            workflow = TextIngestion(
                source,
                products_dir,
                client=client,
                review_store=store,
                exclusions_file=exclusions,
            )
            assert workflow.prepare() == products
            with monkeypatch.context() as patch:
                patch.setattr(
                    assigned_support, "write_existing", counted_write
                )
                recovered = workflow.apply_claims((0,))
            assert recovered.plan_id == stopped.value.plan_id
            assert len(written) == 1, "uncertain historical write was resent"
            (receipt,) = recovered.receipts
            assert receipt == attempted_receipts[0]
            assert receipt.statement_guid == old_support.statement_guid
            assert receipt.primary_hash != old_support.primary_hash
            after = client.wbi.item.get(target.qid).get_json()
            assert after["labels"] == before["labels"]
            assert after["descriptions"] == before["descriptions"]
            (current_birth,) = after["claims"][properties["date_of_birth"]]
            assert {
                key: value
                for key, value in current_birth.items()
                if key != "references"
            } == {
                key: value
                for key, value in old_birth.items()
                if key != "references"
            }
            references = {
                reference["hash"]: reference
                for reference in current_birth["references"]
            }
            assert all(
                references[reference["hash"]] == reference
                for reference in old_birth["references"]
            )
            assert receipt.primary_hash in references
            assert old_support.primary_hash in references
            _synchronize(client)
            frozen = watermark(client)
            workflow.register_source(
                registration,
                expected_digest=registration.digest,
                actor="fictional source curator",
            )
            carried = workflow.prepare_identities()
            assert carried.review.proposals[0].digest == (
                identity_decision.proposal.digest
            )
            repeated = workflow.apply_identities(("gustav",))
            assert (
                repeated.acceptance.snapshot.assignments[mention] == assignment
            )
            assert workflow.apply_claims((0,)) == recovered
            assert store.get_decision(identity_decision.proposal.id) == (
                identity_decision
            )
            assert watermark(client) == frozen
            assert (extractor.calls, critic.calls) == (2, 1)
    finally:
        # Restore this isolated test stack's disposable mode for later cases.
        try:
            if carries_kept_marker(client):
                wbi_helpers.mediawiki_api_call_helper(
                    data={
                        "action": "delete",
                        "title": KEPT_STATE_PAGE,
                        "reason": "fictional retained-ingestion cleanup",
                        "token": client.login.get_edit_token(),
                    },
                    login=client.login,
                )
        finally:
            marker.unlink(missing_ok=True)


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
        rdf_url = (
            f"{client.config.wikibase_url}/wiki/"
            f"Special:EntityData/{target}.ttl"
        )
        rdf = requests.get(
            rdf_url, params={"flavor": "dump", "nocache": "1"}, timeout=30
        )
        assert rdf.status_code == 200 and target in rdf.text
        assert "turtle" in rdf.headers["Content-Type"]
        altered = requests.get(
            rdf_url,
            params={"action": "edit", "flavor": "dump"},
            timeout=30,
        )
        assert altered.status_code == 423
        posted = requests.post(rdf_url, data={"flavor": "dump"}, timeout=30)
        assert posted.status_code == 423
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

    _retained_text_source(client, tmp_path, target, support, monkeypatch)
