"""Current scoped evidence, rather than saved absence, governs additions."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from tests.unit.test_cohort_matching import assignment_request, source
from tests.unit.test_graph_projection import _person
from tests.unit.test_graph_projection import graph as graph
from yizkr.core.write_recovery import write_existing
from yizkr.pipeline import cohort_additions as additions
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
