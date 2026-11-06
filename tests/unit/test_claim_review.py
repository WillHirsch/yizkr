"""Historical consent is scoped to exact meaning, evidence and assignments."""

import copy
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from wikibaseintegrator.datatypes import String

from tests.unit.test_identity_provenance import Wiki
from yizkr.claims import ArtifactRef, Claim, ClaimBatch
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.claim_review import (
    ClaimDecisionInput,
    ClaimSelection,
    pin_claim_review,
    prepare_claim_review,
    record_claim_decisions,
)
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewBlocker,
    ReviewProposal,
    ReviewRequest,
    ReviewSource,
    prepare_review,
)
from yizkr.pipeline.transcribe import Transcription

WHEN = "2026-01-01T12:00:00Z"
TEXT = "Leopold Löbig gave refuge to Klara Löbig in Rekau."


def local(key):
    return {"kind": "local", "local_id": key}


def item(key):
    return {"type": "item", "value": local(key)}


@pytest.fixture
def case(tmp_path, request):
    native = getattr(request, "param", False)
    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    artifact = ArtifactRef.from_bytes(b"fictional refuge account")
    source_item = wiki.wbi.item.new()
    source_item.labels.set("en", "Fictional refuge account")
    source_item.claims.add(
        String(prop_nr=wiki.vocabulary()[0]["media_sha1"], value=artifact.sha1)
    )
    registry.create(
        {"kind": "source", "sha1": artifact.sha1},
        source_item,
        summary="fixture source",
    )
    entities = [
        {
            "id": "leopold",
            "label": "Leopold Löbig",
            "kind": "human",
            "mention_lines": [1],
        },
        {
            "id": "klara",
            "label": "Klara Löbig",
            "kind": "human",
            "mention_lines": [1],
        },
        {
            "id": "rekau",
            "label": "Rekau",
            "kind": "place",
            "mention_lines": [1],
        },
    ]
    batch = ClaimBatch.model_validate(
        {
            "extraction_run": {
                "model_identifier": "fictional",
                "pipeline_version": "test",
                "started_at": WHEN,
            },
            "entities": entities,
            "claims": [
                {
                    "subject": local("leopold"),
                    "property": "gave_refuge_to",
                    "value": item("klara"),
                    "qualifiers": [
                        {"property": "location", "value": item("rekau")},
                        {
                            "property": "point_in_time",
                            "value": {
                                "type": "time",
                                "year": 1900,
                                "precision": "year",
                            },
                        },
                    ],
                    "source": {
                        "artifact": artifact.model_dump(),
                        "transcription_excerpt": {
                            "text": TEXT,
                            "language": "en",
                        },
                    },
                }
            ],
        }
    )
    reading = Transcription(
        artifact=artifact, text=TEXT, language="en", engine="fixture"
    )
    if native:
        from yizkr.claims import SourceSupport
        from yizkr.pipeline.source_context import LocatedReading, SourceContext
        from yizkr.pipeline.transcribe import reading_digest

        support = SourceSupport(
            container=artifact,
            part_id="a" * 64,
            locator={"kind": "page_body", "position": 1},
            reading_sha256=reading_digest(reading),
        )
        reading.source_context = SourceContext(
            container=artifact,
            readings=[
                LocatedReading(
                    start=0, end=len(TEXT), owner=artifact, support=support
                )
            ],
        )
        batch.claims[0].source.support = support
    bindings, proposals = {}, []
    for entity in entities:
        target = wiki.wbi.item.new()
        target.labels.set("en", entity["label"])
        binding = registry.create(
            {"kind": "fictional-claim-target", "name": entity["id"]},
            target,
            summary="fixture target",
        )
        bindings[entity["id"]] = binding
        proposals.append(
            ReviewProposal(
                operation="assign",
                mentions=(
                    MentionKey(
                        artifact_sha1=artifact.sha1, local_id=entity["id"]
                    ),
                ),
                target=binding,
            )
        )
    request = ReviewRequest(
        sources=(ReviewSource(artifact.sha1, batch, reading),),
        proposals=tuple(proposals),
        current_targets=tuple(bindings.values()),
    )
    database = tmp_path / "review.sqlite"
    with ReviewStore(database) as store:
        identity = prepare_review(store, request)
        for proposal in identity.proposals:
            store.decide(
                proposal.id,
                proposal.revision,
                expected_digest=proposal.digest,
                outcome="approve",
                actor="curator",
                time=WHEN,
            )
        accepted = accept_reviewed_assignments(store, request, registry)
        yield SimpleNamespace(
            wiki=wiki,
            registry=registry,
            store=store,
            request=request,
            selection=ClaimSelection(artifact_sha1=artifact.sha1, index=0),
            accepted=accepted,
            bindings=bindings,
            database=database,
        )


def prepare(case, request=None, selections=None):
    return prepare_claim_review(
        case.store,
        request or case.request,
        case.registry,
        selections=selections or (case.selection,),
    )


def approve(case, prepared, **kwargs):
    inputs = [
        ClaimDecisionInput(
            proposal_id=c.proposal.id,
            expected_digest=c.proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
            **kwargs,
        )
        for c in prepared.claims
    ]
    return record_claim_decisions(case.store, prepared, inputs)


def replace_batch(request, batch):
    return replace(
        request, sources=(replace(request.sources[0], batch=batch),)
    )


def test_all_assignment_roles_require_separate_historical_consent(case):
    pages = dict(case.wiki._recovery_pages)
    graph = {
        qid: copy.deepcopy(entity.get_json())
        for qid, entity in case.wiki.wbi.item.store.items()
    }
    prepared = prepare(case)
    claim = prepared.claims[0]
    assert len(claim.roles) == 3
    assert {r.role.split(":")[0] for r in claim.roles} == {
        "subject",
        "object",
        "qualifier",
    }
    assert all(
        case.accepted.snapshot.assignments[r.mention_id] == r.assignment
        for r in claim.roles
    )
    with (
        pytest.raises(ReviewRefused, match="affirmative approval"),
        pin_claim_review(case.store, prepared),
    ):
        pytest.fail("identity consent is not claim approval")
    decisions = approve(case, prepared)
    with ReviewStore(case.database) as reopened:
        current = prepare_claim_review(
            reopened, case.request, case.registry, selections=(case.selection,)
        )
        assert current.claims[0].decision == decisions[0]
        with pin_claim_review(reopened, current):
            pass
    assert pages == case.wiki._recovery_pages
    assert graph == {
        qid: entity.get_json()
        for qid, entity in case.wiki.wbi.item.store.items()
    }


def test_local_renumber_order_and_run_metadata_preserve_meaning(case):
    original = prepare(case)
    approve(case, original)
    raw = case.request.sources[0].batch.model_dump(mode="json")
    names = {"leopold": "b", "klara": "c", "rekau": "a"}

    def rename(value):
        if isinstance(value, dict):
            if value.get("kind") == "local":
                value["local_id"] = names[value["local_id"]]
            for child in value.values():
                rename(child)
        elif isinstance(value, list):
            for child in value:
                rename(child)

    rename(raw)
    for entity in raw["entities"]:
        entity["id"] = names[entity["id"]]
    raw["entities"].reverse()
    raw["claims"][0]["qualifiers"].reverse()
    raw["extraction_run"]["started_at"] = "2026-02-01T12:00:00Z"
    raw["extraction_run"]["model_identifier"] = "different fictional producer"
    raw["claims"][0]["confidence"] = 0.8
    current = replace_batch(case.request, ClaimBatch.model_validate(raw))
    current = replace(
        current,
        proposals=tuple(
            p.model_copy(
                update={
                    "mentions": tuple(
                        m.model_copy(update={"local_id": names[m.local_id]})
                        for m in p.mentions
                    )
                }
            )
            for p in current.proposals
        ),
    )
    for binding in case.bindings.values():
        entity = case.wiki.wbi.item.get(binding.qid)
        entity.labels.set("en", "Curator display")
        write_existing(entity, summary="fixture curator edit")
    repeated = prepare(case, current)
    assert repeated.claims[0].basis == original.claims[0].basis
    assert repeated.claims[0].proposal.id == original.claims[0].proposal.id
    assert repeated.claims[0].decision is not None
    assert repeated.claims[0].provenance != original.claims[0].provenance
    with pin_claim_review(case.store, repeated):
        pass


def test_new_evidence_does_not_reapprove_identity_or_existing_claim(case):
    first = prepare(case)
    approve(case, first)
    batch = case.request.sources[0].batch.model_copy(deep=True)
    added = batch.claims[0].model_copy(
        update={
            "property": "residence",
            "value": batch.claims[0].qualifiers[0].value,
            "qualifiers": [],
        }
    )
    batch.claims.append(added)
    current = replace_batch(case.request, batch)
    prepared = prepare(
        case,
        current,
        (case.selection, case.selection.model_copy(update={"index": 1})),
    )
    assert prepared.claims[0].decision is not None
    assert prepared.claims[0].proposal == first.claims[0].proposal
    assert prepared.claims[1].decision is None
    assert all(
        r.assignment == case.accepted.snapshot.assignments[r.mention_id]
        for c in prepared.claims
        for r in c.roles
    )
    approve(case, prepared)
    with pin_claim_review(
        case.store, prepare(case, current, prepared.selections)
    ):
        pass


@pytest.mark.parametrize(
    "change", ["old_value", "interpretation", "support", "blocked", "revoke"]
)
def test_identity_or_support_material_change_cannot_use_old_consent(
    case, change
):
    original = prepare(case)
    approve(case, original)
    batch = case.request.sources[0].batch.model_copy(deep=True)
    current = case.request
    if change == "old_value":
        batch.claims[0].qualifiers[1].value.year += 1
    elif change == "interpretation":
        batch.entities[0].aliases.append("Leopold")
    elif change == "support":
        batch.claims[0].source.transcription_excerpt.text = "Klara Löbig"
    elif change == "blocked":
        current = replace(
            current,
            blocked=(
                ReviewBlocker(
                    mention=current.proposals[0].mentions[0],
                    reasons=("new validation failure",),
                ),
            ),
        )
    else:
        proposal = original.identity.proposals[0]
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="reject",
            actor="curator",
            time=WHEN,
            expected_decision_revision=1,
        )
    current = replace_batch(current, batch)
    with pytest.raises(ReviewRefused):
        prepare(case, current)


def test_new_warning_has_no_old_claim_approval(case):
    original = prepare(case)
    approve(case, original)
    warning = case.selection.model_copy(
        update={"warnings": ("uncertain historical reading",)}
    )
    revised = prepare(case, selections=(warning,))
    assert revised.claims[0].proposal.id != original.claims[0].proposal.id
    assert revised.claims[0].decision is None
    with (
        pytest.raises(ReviewRefused, match="affirmative"),
        pin_claim_review(case.store, revised),
    ):
        pytest.fail("a new warning needs claim review")


def test_pin_excludes_claim_and_identity_edits_and_detects_stale_decision(
    case,
):
    prepared = prepare(case)
    approve(case, prepared)
    current = prepare(case)
    claim = current.claims[0]
    request = ClaimDecisionInput(
        proposal_id=claim.proposal.id,
        expected_digest=claim.proposal.digest,
        outcome="reject",
        actor="curator",
        time=WHEN,
        expected_decision_revision=1,
    )
    with ReviewStore(case.database) as other:
        other._db.execute("PRAGMA busy_timeout=0")
        with (
            pin_claim_review(case.store, current),
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            record_claim_decisions(other, current, (request,))
        record_claim_decisions(other, current, (request,))
    with (
        pytest.raises(ReviewRefused, match="changed before"),
        pin_claim_review(case.store, current),
    ):
        pytest.fail("the recorded approval was revoked")


def test_failed_decision_packet_is_atomic(case):
    first = prepare(case)
    warning = prepare(
        case,
        selections=(
            case.selection.model_copy(
                update={"warnings": ("review separately",)}
            ),
        ),
    )
    both = replace(first, claims=first.claims + warning.claims)
    inputs = [
        ClaimDecisionInput(
            proposal_id=c.proposal.id,
            expected_digest=c.proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
            expected_decision_revision=123 if i else None,
        )
        for i, c in enumerate(both.claims)
    ]
    with pytest.raises(ReviewRefused, match="changed since"):
        record_claim_decisions(case.store, both, inputs)
    assert all(
        case.store.get_claim_decision(c.proposal.id) is None
        for c in both.claims
    )


def test_bare_qid_cannot_erase_object_role(case):
    batch = case.request.sources[0].batch.model_copy(deep=True)
    raw = batch.claims[0].model_dump(mode="json")
    raw["value"] = {
        "type": "item",
        "value": {"kind": "qid", "qid": case.bindings["klara"].qid},
    }
    batch.claims[0] = Claim.model_validate(raw)
    with pytest.raises(ReviewRefused, match="bare QID"):
        prepare(case, replace_batch(case.request, batch))


def test_depicts_requires_its_explicit_association_confirmation(case):
    # Review storage checks the property independently of packet shape; a
    # source-valid face workflow must still provide its own reading admission.
    prepared = prepare(case)
    basis = copy.deepcopy(prepared.claims[0].basis)
    basis["claim"]["property"] = "depicts"
    proposal = case.store.propose_claim(
        basis, origin={"method": "fictional face review"}
    )
    current = replace(
        prepared,
        claims=(
            replace(
                prepared.claims[0],
                basis=basis,
                proposal=proposal,
                decision=None,
            ),
        ),
    )
    with pytest.raises(ReviewRefused, match="explicit association"):
        approve(case, current)
    decision = approve(case, current, depicts_confirmed=True)[0]
    assert decision.depicts_confirmed


def test_unlocated_or_ambiguous_literal_support_refuses(case):
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    batch.claims[0].source.transcription_excerpt = None
    with pytest.raises(ReviewRefused):
        prepare(case, replace_batch(case.request, batch))
    repeated = source.reading.model_copy(update={"text": TEXT + "\n" + TEXT})
    current = replace(
        case.request, sources=(replace(source, reading=repeated),)
    )
    with pytest.raises(ReviewRefused):
        prepare(case, current)


def test_proven_reading_offset_shift_keeps_claim_consent(case):
    original = prepare(case)
    approve(case, original)
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    for entity in batch.entities:
        entity.mention_lines = [2]
    shifted = replace(
        case.request,
        sources=(
            replace(
                source,
                batch=batch,
                reading=source.reading.model_copy(
                    update={"text": "\n" + TEXT}
                ),
            ),
        ),
    )
    current = prepare(case, shifted)
    assert current.claims[0].proposal == original.claims[0].proposal
    assert current.claims[0].provenance["source"]["start"] == 1
    with pin_claim_review(case.store, current):
        pass


def test_new_rejection_of_active_target_blocks_claim_application(case):
    original = prepare(case)
    approve(case, original)
    first = case.request.proposals[0]
    other = first.model_copy(
        update={"before": {"reason": "distinct review scope"}}
    )
    current = replace(case.request, proposals=(*case.request.proposals, other))
    prepared = prepare_review(case.store, current)
    proposal = next(p for p in prepared.proposals if p.before)
    case.store.decide(
        proposal.id,
        proposal.revision,
        expected_digest=proposal.digest,
        outcome="reject",
        actor="curator",
        time=WHEN,
    )
    with pytest.raises(ReviewRefused, match="unblocked identity"):
        prepare(case, current)


def test_source_hash_without_creation_authority_cannot_supply_approval(case):
    source = case.request.sources[0]
    alias = ArtifactRef.from_bytes(b"fictional renamed source identity")
    item = case.wiki.wbi.item.new()
    item.labels.set("en", "Fictional unregistered source")
    item.claims.add(
        String(
            prop_nr=case.wiki.vocabulary()[0]["media_sha1"], value=alias.sha1
        )
    )
    item.write()
    batch = source.batch.model_copy(deep=True)
    batch.claims[0].source.artifact = alias
    current = replace(
        case.request,
        sources=(
            ReviewSource(
                alias.sha1,
                batch,
                source.reading.model_copy(update={"artifact": alias}),
            ),
        ),
        proposals=(),
    )
    with pytest.raises(ReviewRefused, match="source has no verified creation"):
        prepare(
            case, current, (ClaimSelection(artifact_sha1=alias.sha1, index=0),)
        )


def test_explicit_override_retains_rejected_origin_and_confirmed_replacement(
    tmp_path,
):
    from tests.unit.test_assignment_stages import binding
    from tests.unit.test_review_stages import request as fictional_request
    from yizkr.pipeline.identity_origins import (
        owner_override_evidence,
        verified_owner_override,
    )

    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    wrong, right = binding(wiki, 1), binding(wiki, 2)
    initial = fictional_request()
    key = initial.proposals[0].mentions[0]
    old = ReviewProposal(
        operation="assign",
        mentions=(key,),
        target=wrong,
        origin={"method": "adjudicated", "run_id": "fictional-origin"},
    )
    initial = replace(
        initial, proposals=(old,), current_targets=(wrong, right)
    )
    with ReviewStore(tmp_path / "override.sqlite") as store:
        prepared = prepare_review(store, initial)
        proposal = prepared.proposals[0]
        rejected = store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="reject",
            actor="curator",
            time=WHEN,
        )
        evidence = owner_override_evidence(
            store,
            proposal.id,
            replacement_mention=prepared.mentions[key.key],
            target=right,
            reason="The proposed target is the other source occurrence.",
        )
        replacement = ReviewProposal(
            operation="assign",
            mentions=(key,),
            target=right,
            evidence=evidence,
            origin={"method": "owner_override"},
        )
        current = replace(initial, proposals=(old, replacement))
        reviewed = prepare_review(store, current)
        proposal = next(
            p
            for p in reviewed.proposals
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
        found = verified_owner_override(confirmed, accepted.snapshot)
        assert found.rejected == rejected
        assert found.rejected.proposal.origin["method"] == "adjudicated"
        assert found.target.entity_id == right.entity_id
        assert confirmed.actor == "curator"
        with pytest.raises(ReviewRefused, match="retained decision"):
            verified_owner_override(
                confirmed,
                accepted.snapshot.model_copy(update={"constraints": ()}),
            )
        # A label on a normal decision is never evidence of an override.
        forged = confirmed.model_copy(
            update={
                "proposal": confirmed.proposal.model_copy(
                    update={"basis": {"evidence": {}}}
                )
            }
        )
        with pytest.raises(ReviewRefused, match="lacks material"):
            verified_owner_override(forged, accepted.snapshot)


def test_fresh_same_target_reapproval_preserves_assignment_origin(case):
    original = prepare(case)
    approve(case, original)
    batch = case.request.sources[0].batch.model_copy(deep=True)
    batch.claims[0].qualifiers[1].value.year += 1
    current = replace_batch(case.request, batch)
    review = prepare_review(case.store, current)
    for proposal in review.proposals:
        prior = case.store.get_decision(proposal.id)
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="curator",
            time=WHEN,
            expected_decision_revision=prior.revision,
        )
    accepted = accept_reviewed_assignments(case.store, current, case.registry)
    assert accepted.snapshot.assignments == case.accepted.snapshot.assignments
    pending = prepare(case, current)
    assert pending.claims[0].proposal != original.claims[0].proposal
    assert pending.claims[0].decision is None
    approve(case, pending)
    with pin_claim_review(case.store, prepare(case, current)):
        pass


@pytest.mark.parametrize("case", [True], indirect=True)
def test_native_support_checks_reading_and_preserves_proven_offset_shift(case):
    from yizkr.pipeline.transcribe import reading_digest

    original = prepare(case)
    approve(case, original)
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    reading = source.reading.model_copy(deep=True)
    reading.text = "\n" + reading.text
    span = reading.source_context.readings[0]
    span.start += 1
    span.end += 1
    span.support.reading_sha256 = reading_digest(reading)
    batch.claims[0].source.support = span.support.model_copy(deep=True)
    for entity in batch.entities:
        entity.mention_lines = [2]
    current = replace(
        case.request, sources=(replace(source, batch=batch, reading=reading),)
    )
    shifted = prepare(case, current)
    assert shifted.claims[0].proposal == original.claims[0].proposal
    with pin_claim_review(case.store, shifted):
        pass
    span.support.reading_sha256 = "b" * 64
    with pytest.raises(ReviewRefused):
        prepare(case, current)


@pytest.mark.parametrize("negative", ["rejected_same", "different"])
def test_current_negative_cannot_split_accepted_component(tmp_path, negative):
    from tests.unit.test_assignment_stages import assigned_request, binding

    wiki = Wiki(detached=True)
    registry = CreationRegistry(wiki)
    request = assigned_request(binding(wiki))
    source = request.sources[0]
    text = "Leopold Löbig, born in 1900.\nKlara Löbig."
    batch = source.batch.model_copy(deep=True)
    batch.claims = [
        Claim.model_validate(
            {
                "subject": local("leopold"),
                "property": "date_of_birth",
                "value": {"type": "time", "year": 1900, "precision": "year"},
                "source": {
                    "artifact": {"sha1": source.artifact_sha1},
                    "transcription_excerpt": {
                        "text": text.splitlines()[0],
                        "language": "en",
                    },
                },
            }
        )
    ]
    request = replace(
        request,
        sources=(
            replace(
                source,
                batch=batch,
                reading=source.reading.model_copy(update={"text": text}),
            ),
        ),
    )
    item_ = wiki.wbi.item.new()
    item_.labels.set("en", "Fictional birth and family note")
    item_.claims.add(
        String(
            prop_nr=wiki.vocabulary()[0]["media_sha1"],
            value=source.artifact_sha1,
        )
    )
    registry.create(
        {"kind": "source", "sha1": source.artifact_sha1},
        item_,
        summary="fixture source",
    )
    selection = ClaimSelection(artifact_sha1=source.artifact_sha1, index=0)
    with ReviewStore(tmp_path / "negative.sqlite") as store:
        identity = prepare_review(store, request)
        for p in identity.proposals:
            store.decide(
                p.id,
                p.revision,
                expected_digest=p.digest,
                outcome="approve",
                actor="curator",
                time=WHEN,
            )
        accepted = accept_reviewed_assignments(store, request, registry)
        assert (
            len({a.entity_id for a in accepted.snapshot.assignments.values()})
            == 1
        )
        current = prepare_claim_review(
            store, request, registry, selections=(selection,)
        )
        claim = current.claims[0]
        record_claim_decisions(
            store,
            current,
            (
                ClaimDecisionInput(
                    proposal_id=claim.proposal.id,
                    expected_digest=claim.proposal.digest,
                    outcome="approve",
                    actor="curator",
                    time=WHEN,
                ),
            ),
        )
        same = next(p for p in identity.proposals if p.operation == "same")
        if negative == "rejected_same":
            store.decide(
                same.id,
                same.revision,
                expected_digest=same.digest,
                outcome="reject",
                actor="curator",
                time=WHEN,
                expected_decision_revision=1,
            )
        else:
            proposed = ReviewProposal(
                operation="different", mentions=request.proposals[1].mentions
            )
            request = replace(
                request, proposals=(*request.proposals, proposed)
            )
            review = prepare_review(store, request)
            different = next(
                p for p in review.proposals if p.operation == "different"
            )
            store.decide(
                different.id,
                different.revision,
                expected_digest=different.digest,
                outcome="approve",
                actor="curator",
                time=WHEN,
            )
        pages = dict(wiki._recovery_pages)
        with pytest.raises(ReviewRefused, match="identity|split"):
            prepare_claim_review(
                store, request, registry, selections=(selection,)
            )
        assert wiki._recovery_pages == pages
        assert registry.assignment_snapshot() == accepted.snapshot


def test_new_same_cannot_merge_an_unselected_accepted_entity(case):
    source = case.request.sources[0]
    batch = source.batch.model_copy(deep=True)
    # The selected addition uses Leopold and the place, not Klara; Klara is
    # still relevant because the new identity proposal joins her to Leopold.
    batch.claims.append(
        batch.claims[0].model_copy(
            update={
                "property": "residence",
                "value": batch.claims[0].qualifiers[0].value,
                "qualifiers": [],
            }
        )
    )
    current = replace_batch(case.request, batch)
    same = ReviewProposal(
        operation="same",
        mentions=(
            current.proposals[0].mentions[0],
            current.proposals[1].mentions[0],
        ),
    )
    current = replace(current, proposals=(*current.proposals, same))
    review = prepare_review(case.store, current)
    for proposal in review.proposals:
        previous = case.store.get_decision(proposal.id)
        rejecting_old_target = (
            proposal.operation == "assign"
            and proposal.target.entity_id == case.bindings["klara"].entity_id
        )
        case.store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            expected_decision_revision=previous.revision if previous else None,
            outcome="reject" if rejecting_old_target else "approve",
            actor="curator",
            time=WHEN,
        )
    with pytest.raises(ReviewRefused, match="unsupported"):
        prepare(
            case, current, (case.selection.model_copy(update={"index": 1}),)
        )
    assert case.registry.assignment_snapshot() == case.accepted.snapshot


@pytest.mark.parametrize("fault", ["mention", "entity", "binding", "revision"])
def test_saved_claim_role_requires_its_exact_assignment_binding(case, fault):
    from yizkr.pipeline.claim_review import ClaimRole

    original = prepare(case).claims[0].roles[0]
    raw = original.model_dump(mode="json")
    if fault == "mention":
        raw["mention_id"] = "f" * 32
    elif fault == "entity":
        raw["assignment"]["entity_id"] = "f" * 32
    elif fault == "binding":
        raw["binding"]["binding"]["entity_id"] = "f" * 32
    else:
        raw["assignment"]["mention_revision"] += 1
    with pytest.raises(ValueError, match="claim role"):
        ClaimRole.model_validate(raw)
    assert (
        ClaimRole.model_validate(original.model_dump(mode="json")) == original
    )
