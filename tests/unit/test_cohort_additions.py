"""Current scoped evidence, rather than saved absence, governs additions."""

from contextlib import contextmanager, nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.unit.test_cohort_matching import assignment_request, source
from tests.unit.test_graph_projection import _person
from tests.unit.test_graph_projection import graph as graph
from tests.unit.test_reconcile import fictional_births
from yizkr.core.write_recovery import CreationRegistry, write_existing
from yizkr.pipeline import cohort_additions as additions
from yizkr.pipeline import cohort_context, cohort_provenance, reconcile
from yizkr.pipeline.assignment_stages import accept_reviewed_assignments
from yizkr.pipeline.identity_review import ReviewRefused, ReviewStore
from yizkr.pipeline.review_stages import (
    MentionKey,
    ReviewProposal,
    ReviewRequest,
)


def new_request(*sources):
    return ReviewRequest(
        sources=sources,
        proposals=tuple(
            ReviewProposal(
                operation="new_entity",
                mentions=(
                    MentionKey(
                        artifact_sha1=s.artifact_sha1, local_id="gustav"
                    ),
                ),
            )
            for s in sources
        ),
    )


def approve(store, prepared):
    for proposal in prepared.review.proposals:
        previous = store.get_decision(proposal.id)
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional curator",
            time="2026-01-01T12:00:00Z",
            expected_decision_revision=previous.revision if previous else None,
        )


def refresh(wiki, service):
    service.revisions = wiki.entity_revisions(wiki.list_entity_ids())


@pytest.fixture
def previous_empty_birth_facts():
    """The saved review basis before plural birth evidence was introduced."""
    current = cohort_context.identity_facts

    def previous(projection, qid):
        assert all(
            not row.births for row in projection.roster if row.qid == qid
        )
        facts = []
        for value in current(projection, qid):
            old = dict(value)
            old.pop("births", None)
            old["birth"] = ""
            facts.append(old)
        return tuple(facts)

    return previous


@pytest.mark.parametrize("operation", ["assign", "new_entity"])
def test_empty_birth_upgrade_preserves_pending_approval_and_creation_recovery(
    graph, tmp_path, monkeypatch, previous_empty_birth_facts, operation
):
    wiki, registry, target, _, service = graph
    request = (
        assignment_request(target.binding, (source(),))
        if operation == "assign"
        else new_request(source())
    )
    monkeypatch.setattr(
        additions,
        "protected_mutation",
        lambda *args, **kwargs: nullcontext(
            SimpleNamespace(evidence={}, assert_active=lambda: None)
        ),
    )
    monkeypatch.setattr(additions, "unique_source_qids", lambda *args: {})
    monkeypatch.setattr(
        cohort_provenance,
        "publish_cohort_assignment",
        lambda *args, **kwargs: None,
    )
    apply_args = {
        "state_path": tmp_path / "guard.json",
        "operation_id": "fictional-upgrade-recovery",
        "record_dir": tmp_path / "records",
    }
    path = tmp_path / "review.sqlite"
    expected_qid = target.binding.qid
    with monkeypatch.context() as old, ReviewStore(path) as store:
        old.setattr(
            cohort_context, "identity_facts", previous_empty_birth_facts
        )
        prepared = additions.prepare_cohort_additions(store, request, registry)
        (proposal,) = prepared.review.proposals
        (candidate,) = proposal.basis["evidence"]["cohort_candidates"][
            "candidates"
        ]
        assert candidate["qid"] == target.binding.qid
        assert all(facts["birth"] == "" for facts in candidate["facts"])
        approve(store, prepared)
        decision = store.get_decision(proposal.id)
        if operation == "new_entity":

            def interrupted(*args, **kwargs):
                raise RuntimeError("fictional interruption before assignment")

            old.setattr(additions, "accept_reviewed_assignments", interrupted)
            with pytest.raises(RuntimeError, match="before assignment"):
                additions.apply_cohort_additions(
                    store, request, registry, **apply_args
                )
            (authorization,) = registry.reviewed_creation_authorizations()
            assert authorization.decision == decision
            binding = registry.reviewed_creation_binding(authorization)
            assert binding is not None
            expected_qid = binding.qid
        assert not registry.assignment_snapshot().assignments

    refresh(wiki, service)
    registry = CreationRegistry(wiki)
    entity_ids = set(wiki.list_entity_ids())
    with ReviewStore(path) as reopened:
        current = additions.prepare_cohort_additions(
            reopened, request, registry
        )
        assert current.review.proposals == (proposal,)
        assert reopened.get_decision(proposal.id) == decision
        (application,) = current.review.projection.applications
        assert (
            application.status == "applied"
            and application.decision == decision
        )
        result = additions.apply_cohort_additions(
            reopened, request, registry, **apply_args
        )
        (assignment,) = result.acceptance.snapshot.assignments.values()
        (reference,) = assignment.decision_references
        assert (reference.id, reference.revision) == (
            decision.id,
            decision.revision,
        )
        assert (
            registry.binding(assignment.entity_id).binding.qid == expected_qid
        )
        assert reopened.list_proposals() == [proposal]
    assert set(wiki.list_entity_ids()) == entity_ids


@pytest.mark.parametrize("operation", ["assign", "new_entity"])
def test_new_birth_evidence_requires_review_after_empty_birth_approval(
    graph, tmp_path, monkeypatch, previous_empty_birth_facts, operation
):
    _, registry, target, _, _ = graph
    request = (
        assignment_request(target.binding, (source(),))
        if operation == "assign"
        else new_request(source())
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        with monkeypatch.context() as old:
            old.setattr(
                cohort_context, "identity_facts", previous_empty_birth_facts
            )
            prepared = additions.prepare_cohort_additions(
                store, request, registry
            )
            approve(store, prepared)
        monkeypatch.setattr(
            reconcile,
            "fetch_birth_evidence",
            lambda *args: {
                target.binding.qid: fictional_births(target.binding.qid)
            },
        )
        current = additions.prepare_cohort_additions(store, request, registry)
        assert (
            current.review.proposals[0].digest
            != prepared.review.proposals[0].digest
        )
        assert (
            current.review.projection.applications[0].status == "needs_review"
        )
        with pytest.raises(ReviewRefused, match="current, unblocked"):
            additions._approved_scope(current, None)
        assert not registry.assignment_snapshot().assignments
        assert registry.reviewed_creation_authorizations() == ()


def test_rename_and_unrelated_changes_preserve_candidate_review(
    graph, tmp_path
):
    wiki, registry, target, unrelated, service = graph
    request = assignment_request(target.binding, (source(),))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        entity = wiki.wbi.item.get(target.binding.qid)
        entity.labels.set("en", "Gustl")
        entity.aliases.set("en", [])
        write_existing(entity, summary="fictional display rename")
        other = wiki.wbi.item.get(unrelated.qid)
        other.descriptions.set("en", "fictional unrelated update")
        write_existing(other, summary="fictional unrelated description")
        refresh(wiki, service)
        current = additions.prepare_cohort_additions(store, request, registry)
        assert current.context.digest != prepared.context.digest
        assert current.review.proposals == prepared.review.proposals
        assert all(
            a.status == "applied"
            for a in current.review.projection.applications
        )
        additions._approved_scope(current, None)


def test_new_candidate_invalidates_old_approval_without_old_revision_change(
    graph, tmp_path
):
    wiki, registry, _, _, service = graph
    request = new_request(source())
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        old = dict(prepared.context.projection.entity_revisions)
        new = _person(registry, "Gustav Löbig")
        registry.index_binding(new)
        service.people = (*service.people, new)
        refresh(wiki, service)
        assert all(
            service.revisions[qid] == revision for qid, revision in old.items()
        )
        current = additions.prepare_cohort_additions(store, request, registry)
        assert (
            current.review.proposals[0].digest
            != prepared.review.proposals[0].digest
        )
        assert (
            current.review.projection.applications[0].status == "needs_review"
        )
        with pytest.raises(ReviewRefused, match="current, unblocked"):
            additions._approved_scope(current, None)


def test_accepted_identity_does_not_reopen_for_new_alternatives(
    graph, tmp_path
):
    wiki, registry, target, _, service = graph
    request = assignment_request(target.binding, (source(),))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        original = accept_reviewed_assignments(
            store, prepared.request, registry
        ).snapshot
        new = _person(registry, "Gustav Löbig")
        service.people = (*service.people, new)
        refresh(wiki, service)
        current = additions.prepare_cohort_additions(store, request, registry)
        assert current.review.proposals == prepared.review.proposals
        additions._approved_scope(current, None)
        assert registry.assignment_snapshot() == original


def test_uncovered_kindless_item_refuses_even_fresh_creation_approval(
    graph, tmp_path
):
    wiki, registry, _, _, service = graph
    unknown = _person(registry, "Klara Löbig")
    refresh(wiki, service)
    with ReviewStore(tmp_path / "review.sqlite") as store:
        request = new_request(source())
        prepared = additions.prepare_cohort_additions(store, request, registry)
        assert prepared.context.uncovered_qids == (unknown.qid,)
        approve(store, prepared)
        current = additions.prepare_cohort_additions(store, request, registry)
        with pytest.raises(ReviewRefused, match="coverage is incomplete"):
            additions._approved_scope(current, None)
        assert registry.reviewed_creation_authorizations() == ()


def test_multiple_creations_require_separate_current_candidate_checks(
    graph, tmp_path
):
    _, registry, _, _, _ = graph
    with ReviewStore(tmp_path / "review.sqlite") as store:
        request = new_request(source(), source("naturalisation-1952"))
        approve(
            store, additions.prepare_cohort_additions(store, request, registry)
        )
        current = additions.prepare_cohort_additions(store, request, registry)
        with pytest.raises(ReviewRefused, match="at most one"):
            additions._approved_scope(current, None)


def test_source_change_stales_review_and_omitted_proposal_is_refreshed(
    graph, tmp_path
):
    wiki, registry, _, _, service = graph
    request = new_request(source())
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        source_now = source()
        source_now.batch.entities[0].aliases.append("Gustl")
        current = additions.prepare_cohort_additions(
            store,
            replace(request, sources=(source_now,), proposals=()),
            registry,
        )
        assert (
            current.review.projection.applications[0].status == "needs_review"
        )
        assert len(current.request.proposals) == 1


def test_creation_retry_keeps_original_approval_receipt_and_assignment(
    graph, tmp_path, monkeypatch
):
    from yizkr.pipeline import cohort_provenance

    wiki, registry, _, _, service = graph
    events = []

    @contextmanager
    def guard(*args, **kwargs):
        class Fence:
            evidence = {
                "protocol": "fictional-guard",
                "epoch": 1,
                "operation_id": "fictional",
            }

            def assert_active(self):
                events.append("check")

        events.append("begin")
        yield Fence()
        events.append("end")

    monkeypatch.setattr(additions, "protected_mutation", guard)
    monkeypatch.setattr(additions, "unique_source_qids", lambda *args: {})
    monkeypatch.setattr(
        cohort_provenance,
        "publish_cohort_assignment",
        lambda *args, **kwargs: None,
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        request = new_request(source())
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        original = store.get_decision(prepared.review.proposals[0].id)
        apply_args = {
            "state_path": tmp_path / "guard.json",
            "operation_id": "fictional",
            "record_dir": tmp_path / "records",
        }
        result = additions.apply_cohort_additions(
            store, request, registry, **apply_args
        )
        (assignment,) = result.acceptance.snapshot.assignments.values()
        assert assignment.decision_references[0].id == original.id
        assert [p.operation for p in store.list_proposals()] == ["new_entity"]
        qid = registry.binding(assignment.entity_id).binding.qid
        entity = wiki.wbi.item.get(qid)
        entity.labels.set("en", "Gustl")
        write_existing(entity, summary="fictional rename after creation")
        refresh(wiki, service)
        writes = wiki.wbi.item.writes
        retry = additions.apply_cohort_additions(
            store, request, registry, **apply_args
        )
        assert retry.acceptance.snapshot == result.acceptance.snapshot
        assert wiki.wbi.item.writes == writes
        assert wiki.wbi.item.get(qid).labels.get("en") == "Gustl"
        assert events[0] == "begin" and events[-1] == "end"


def test_external_admission_refuses_before_creation_or_assignment_effects(
    graph, tmp_path, monkeypatch
):
    wiki, registry, _, _, _ = graph
    events = []
    guarded = False

    @contextmanager
    def guard(*args, **kwargs):
        nonlocal guarded
        guarded = True
        events.append("guard")
        try:
            yield None
        finally:
            guarded = False
            events.append("released")

    def source_check(*args):
        assert guarded
        events.append("source-check")
        return {}

    def admission():
        assert guarded
        assert events == ["guard", "source-check"]
        raise ReviewRefused("source became excluded during preparation")

    with ReviewStore(tmp_path / "review.sqlite") as store:
        request = new_request(source())
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        before = dict(wiki._recovery_pages)
        writes = wiki.wbi.item.writes
        assignments = registry.assignment_snapshot()
        monkeypatch.setattr(additions, "protected_mutation", guard)
        monkeypatch.setattr(additions, "unique_source_qids", source_check)
        with pytest.raises(ReviewRefused, match="source became excluded"):
            additions.apply_cohort_additions(
                store,
                request,
                registry,
                state_path=tmp_path / "guard.json",
                operation_id="fictional-admission",
                record_dir=tmp_path / "records",
                admit=admission,
            )
        assert events == ["guard", "source-check", "released"]
        assert not guarded
        assert wiki.wbi.item.writes == writes
        assert wiki._recovery_pages == before
        assert registry.assignment_snapshot() == assignments
        assert registry.reviewed_creation_authorizations() == ()
        assert not (tmp_path / "records").exists()


def test_refresh_preserves_original_proposal_producer(graph, tmp_path):
    _, registry, target, _, _ = graph
    from yizkr.core.provenance import ProcessRecord, StageProvenance

    producer = ProcessRecord(
        kind="fictional-cohort-adjudication",
        stages=(StageProvenance(stage="adjudicate", process="manual"),),
    )
    origin = {
        "method": "adjudicated",
        "process": "authored-fictional-decision",
        "provenance": producer.model_dump(mode="json"),
    }
    base = assignment_request(target.binding, (source(),))
    request = replace(
        base,
        proposals=(base.proposals[0].model_copy(update={"origin": origin}),),
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        first = additions.prepare_cohort_additions(store, request, registry)
        approve(store, first)
        again = additions.prepare_cohort_additions(
            store, first.request, registry
        )
        assert again.review.proposals == first.review.proposals
        assert again.review.proposals[0].origin["proposal_origin"] == origin
        assert (
            again.review.proposals[0].origin["provenance"]
            == origin["provenance"]
        )


def test_missing_reading_cannot_accept_an_assignment(
    graph, tmp_path, monkeypatch
):
    wiki, registry, target, _, _ = graph
    request = assignment_request(
        target.binding, (replace(source(), reading=None),)
    )
    with ReviewStore(tmp_path / "review.sqlite") as store:
        prepared = additions.prepare_cohort_additions(store, request, registry)
        approve(store, prepared)
        current = additions.prepare_cohort_additions(store, request, registry)
        before = dict(wiki._recovery_pages)
        with pytest.raises(
            ReviewRefused, match="complete source correspondence"
        ):
            additions._approved_scope(current, None)
        assert registry.assignment_snapshot().assignments == {}
        assert wiki._recovery_pages == before


def test_creation_cannot_replace_an_accepted_identity(graph, tmp_path):
    _, registry, target, _, _ = graph
    request = assignment_request(target.binding, (source(),))
    with ReviewStore(tmp_path / "review.sqlite") as store:
        first = additions.prepare_cohort_additions(store, request, registry)
        approve(store, first)
        accepted = accept_reviewed_assignments(
            store, first.request, registry
        ).snapshot
        old = store.get_decision(first.review.proposals[0].id)
        store.decide(
            old.proposal.id,
            old.proposal.revision,
            expected_digest=old.proposal.digest,
            outcome="reject",
            actor="fictional curator",
            time="2026-01-02T12:00:00Z",
            expected_decision_revision=old.revision,
        )
        new = additions.prepare_cohort_additions(
            store, new_request(source()), registry
        )
        proposal = next(
            p for p in new.review.proposals if p.operation == "new_entity"
        )
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional curator",
            time="2026-01-02T12:00:00Z",
        )
        current = additions.prepare_cohort_additions(
            store, new_request(source()), registry
        )
        with pytest.raises(ReviewRefused, match="cannot be reassigned"):
            additions._approved_scope(current, None)
        assert registry.assignment_snapshot() == accepted
        assert registry.reviewed_creation_authorizations() == ()


def test_existing_plain_assignment_keeps_its_origin_in_mixed_cohort(
    graph, tmp_path, monkeypatch
):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from tests.unit.test_cohort_matching import decide
    from yizkr.pipeline import cohort_provenance

    _, registry, target, _, _ = graph
    first, second = source(), source("naturalisation-1952")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        plain = assignment_request(target.binding, (first,))
        decide(store, plain)
        before = accept_reviewed_assignments(store, plain, registry).snapshot
        old_id, old_assignment = next(iter(before.assignments.items()))
        current = assignment_request(target.binding, (second,))
        request = replace(current, sources=(first, second))
        prepared = additions.prepare_cohort_additions(store, request, registry)
        proposal = next(
            p for p in prepared.review.proposals if p.mentions != (old_id,)
        )
        store.decide(
            proposal.id,
            proposal.revision,
            expected_digest=proposal.digest,
            outcome="approve",
            actor="fictional curator",
            time="2026-01-02T12:00:00Z",
        )
        published = []
        monkeypatch.setattr(
            additions,
            "protected_mutation",
            lambda *args, **kwargs: nullcontext(
                SimpleNamespace(evidence={}, assert_active=lambda: None)
            ),
        )
        monkeypatch.setattr(additions, "unique_source_qids", lambda *args: {})
        monkeypatch.setattr(
            cohort_provenance,
            "publish_cohort_assignment",
            lambda client, mention, **kwargs: published.append(mention),
        )
        result = additions.apply_cohort_additions(
            store,
            request,
            registry,
            state_path=tmp_path / "guard.json",
            operation_id="fictional",
            record_dir=tmp_path / "records",
        )
        assert result.acceptance.snapshot.assignments[old_id] == old_assignment
        assert published == [proposal.mentions[0]]
        assert len(result.acceptance.snapshot.assignments) == 2
